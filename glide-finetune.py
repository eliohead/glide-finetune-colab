from torch.cuda.amp import autocast
import os
from ctypes import resize
from pickle import FALSE
from posixpath import expanduser

import numpy as np
import PIL
import torch as th
from glide_text2im.gaussian_diffusion import (GaussianDiffusion,
                                              get_named_beta_schedule)
from glide_text2im.model_creation import create_gaussian_diffusion
from glide_text2im.respace import SpacedDiffusion, space_timesteps
from glide_text2im.text2im_model import Text2ImUNet
from tqdm import tqdm, trange

import util
import wandb
from loader import TextImageDataset
import bitsandbytes as bnb


# def create_gaussian_diffusion(timestep_respacing):
def pred_to_pil(pred: th.Tensor) -> PIL.Image:
    scaled = ((pred+ 1)*127.5).round().clamp(0,255).to(th.uint8).cpu()
    reshaped = scaled.permute(2, 0, 3, 1).reshape([pred.shape[2], -1, 3])
    return PIL.Image.fromarray(reshaped.numpy())

@autocast(enabled=True)
def train_step(
    glide_model: Text2ImUNet,
    glide_diffusion: SpacedDiffusion,
    glide_options: dict,
    prompt: str, # the prompt to use for the image
    x_img: th.Tensor, # the image to be generated
    batch_size: int,
    device: str,
):
    model_kwargs = util.prompt_to_model_kwargs(glide_model, glide_options, prompt, batch_size, device) # get the prompt embedding attached to kwargs
    current_respacing = int(glide_options['timestep_respacing']) # the current respacing
    respace_multiplier = glide_options['diffusion_steps'] // current_respacing # multiply the respacing by this before passing to the model
    full_batch_size = batch_size * 2  # bs is double for uncond/mask tokens
    x_img = x_img.repeat((full_batch_size, 1, 1, 1)) # repeat the image for the full batch size
    noise = th.randn_like(x_img) # the noise to be added to the image
    t = th.randint(0, current_respacing-1, (full_batch_size,), device=device) # the timestep to use, respecting the respacing
    scaled_t = t * respace_multiplier # scale the timesteps to e.g. 1000 before feeding into the model
    x_t = glide_diffusion.q_sample(x_img, t, noise=noise) # sample from q(x_t | x_0, t)
    model_output = glide_model(x_t, scaled_t, **model_kwargs).requires_grad_(True) # get the model output.
    # pred = model_output[..., 3:, :, :] # get the prediction from the model output
    # pred_pil = pred_to_pil(pred) # convert to PIL image
    # pred_pil.save('pred.png') # save the image
    epsilon = model_output[..., :3, :, :] # epsilon is [bs, 3, h, w]
    return th.nn.functional.mse_loss(epsilon, noise) # the loss is the mean squared error between the image and the predicted image


def run_glide_finetune(
    data_dir="./data",
    batch_size=1,
    grad_acc=1,
    guidance_scale=4.0,
    learning_rate=2e-5,
    dropout=0.0,
    timestep_respacing="100",
    side_x=64,
    side_y=64,
    resume_ckpt="",
    checkpoints_dir="./finetune_checkpoints",
    outputs_dir="./outputs",
    use_fp16=False, # Tends to cause issues,not sure why as the paper states fp16 is stable.
    device="cpu",
    freeze_transformer=False,
    freeze_diffusion=False,
    weight_decay=0.0,
    project_name="glide_finetune",
):
    # Create the checkpoint/output directories
    os.makedirs(checkpoints_dir, exist_ok=True)
    os.makedirs(outputs_dir, exist_ok=True)

    # Start wandb logging
    wandb_run = util.wandb_setup(
        batch_size=batch_size,
        grad_acc=grad_acc,
        side_x=side_x,
        side_y=side_y,
        learning_rate=learning_rate,
        guidance_scale=guidance_scale,
        use_fp16=use_fp16,
        device=device,
        data_dir=data_dir,
        base_dir=checkpoints_dir,
        project_name=project_name,
    )

    # Dataset/dataloader setup
    dataset = TextImageDataset(
        folder=data_dir,
        shuffle=True,
        batch_size=batch_size,
        side_x=side_x,
        side_y=side_y,
        device=device,
        force_reload=False,
    )
    assert len(dataset) > 0, "Dataset is empty"
    print(f"Dataset contains {len(dataset)} images")
    dataloader = th.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True, num_workers=0
    )

    # Model setup
    glide_model, glide_diffusion, glide_options = util.load_base_model(
        glide_path=resume_ckpt,
        use_fp16=use_fp16,
        dropout=dropout,
        timestep_respacing=timestep_respacing,
        freeze_transformer=freeze_transformer,
        freeze_diffusion=freeze_diffusion,
    )
    glide_model.to(device)
    glide_model.half() #
    # Optimizer setup
    # adam_optimizer = th.optim.Adam(
    adam_optimizer = bnb.optim.Adam( # use bitsandbytes adam, supports 8-bit
        [x for x in glide_model.parameters() if x.requires_grad],
        lr=learning_rate,
        weight_decay=weight_decay,
    )
    # sgd_optimizer = th.optim.SGD(
    #     [x for x in glide_model.parameters() if x.requires_grad],
    #     lr=learning_rate,
    #     momentum=0.9,
    #     weight_decay=weight_decay,
    # )

    # Training setup
    current_loss = 0  # hold gradients until all are accumulated
    accum_losses = []  # for determining grad_acc division

    # Training loop
    for i, (captions, images) in tqdm(enumerate(dataloader), total=len(dataloader)):
        images = images.to(device)
        for prompt, image in zip(captions, images):  # TODO refactor to use dataloader
            loss = train_step(glide_model, glide_diffusion, glide_options, prompt, image, batch_size, device)
            current_loss += loss.item()  # sum up loss over grad_acc batches
            loss.backward() # backpropagate the loss
            if i % grad_acc == grad_acc - 1:
                adam_optimizer.step() # update the model parameters
                adam_optimizer.zero_grad() # NOW zero the gradients, get it?
                current_loss /= grad_acc  # finally average the loss over the grad_acc
                accum_losses.append(current_loss)
                wandb_run.log({"loss": current_loss})
                tqdm.write(f"Loss: {current_loss:.12f}")
                current_loss = 0
            if i % 5000 == 0:
                th.save(
                    glide_model.state_dict(),
                    os.path.join(checkpoints_dir, f"glide-ft-{i}.pt"),
                )
                th.save(
                    glide_model.state_dict(),
                    os.path.join(checkpoints_dir, f"glide-ft.pt"),
                )
                print(f"Saved checkpoint {i} to {checkpoints_dir}/glide-ft-{i}.pt")
            # current_loss = 0  # reset for next grad_acc


if __name__ == "__main__":
    # CUDA/CPU setup
    has_cuda = th.cuda.is_available()
    device = th.device("cpu") if not has_cuda else "cuda"
    run_glide_finetune(
        data_dir=expanduser("~/datasets/text-image-datasets/COCO"),
        batch_size=1,
        grad_acc=1,
        guidance_scale=4.0,
        learning_rate=2e-5,
        dropout=0.0,
        timestep_respacing="100",
        side_x=64, side_y=64,
        resume_ckpt="",
        checkpoints_dir="./round2_checkpoints",
        outputs_dir="./outputs",
        use_fp16=True,
        device="cuda",
        freeze_transformer=False,
        freeze_diffusion=False,
        weight_decay=0.0,
        project_name="glide_finetune",
    )
