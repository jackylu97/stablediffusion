import sys
import os
import torch
import numpy as np
import gradio as gr
from PIL import Image
from omegaconf import OmegaConf
from einops import repeat, rearrange
from pytorch_lightning import seed_everything
from imwatermark import WatermarkEncoder

from scripts.txt2img import put_watermark
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.data.util import AddMiDaS

torch.set_grad_enabled(False)


def initialize_model(config, ckpt):
    config = OmegaConf.load(config)
    model = instantiate_from_config(config.model)
    model.load_state_dict(torch.load(ckpt)["state_dict"], strict=False)

    device = torch.device(
        "cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = model.to(device)
    sampler = DDIMSampler(model)
    return sampler


def make_batch_sd(
        image,
        txt,
        device,
        num_samples=1,
        model_type="dpt_hybrid"
):
    image = np.array(image.convert("RGB"))
    image = torch.from_numpy(image).to(dtype=torch.float32) / 127.5 - 1.0
    # sample['jpg'] is tensor hwc in [-1, 1] at this point
    midas_trafo = AddMiDaS(model_type=model_type)
    batch = {
        "jpg": image,
        "txt": num_samples * [txt],
    }
    batch = midas_trafo(batch)
    batch["jpg"] = rearrange(batch["jpg"], 'h w c -> 1 c h w')
    batch["jpg"] = repeat(batch["jpg"].to(device=device),
                          "1 ... -> n ...", n=num_samples)
    batch["midas_in"] = repeat(torch.from_numpy(batch["midas_in"][None, ...]).to(
        device=device), "1 ... -> n ...", n=num_samples)
    return batch


def paint(sampler, image, prompt, t_enc, seed, scale, num_samples=1, callback=None,
          do_full_sample=False):
    device = torch.device(
        "cuda") if torch.cuda.is_available() else torch.device("cpu")
    model = sampler.model
    seed_everything(seed)

    with torch.no_grad(),\
            torch.autocast("cuda"):
        batch = make_batch_sd(
            image, txt=prompt, device=device, num_samples=num_samples)
        z = model.get_first_stage_encoding(model.encode_first_stage(
            batch[model.first_stage_key]))  # move to latent space
        c = model.cond_stage_model.encode(batch["txt"])
        c_cat = list()
        for ck in model.concat_keys:
            cc = batch[ck]
            cc = model.depth_model(cc)
            depth_min, depth_max = torch.amin(cc, dim=[1, 2, 3], keepdim=True), torch.amax(cc, dim=[1, 2, 3],
                                                                                           keepdim=True)
            display_depth = (cc - depth_min) / (depth_max - depth_min)
            depth_image = Image.fromarray(
                (display_depth[0, 0, ...].cpu().numpy() * 255.).astype(np.uint8))
            cc = torch.nn.functional.interpolate(
                cc,
                size=z.shape[2:],
                mode="bicubic",
                align_corners=False,
            )
            depth_min, depth_max = torch.amin(cc, dim=[1, 2, 3], keepdim=True), torch.amax(cc, dim=[1, 2, 3],
                                                                                           keepdim=True)
            cc = 2. * (cc - depth_min) / (depth_max - depth_min) - 1.
            c_cat.append(cc)
        c_cat = torch.cat(c_cat, dim=1)
        # cond
        cond = {"c_concat": [c_cat], "c_crossattn": [c]}

        # uncond cond
        uc_cross = model.get_unconditional_conditioning(num_samples, "")
        uc_full = {"c_concat": [c_cat], "c_crossattn": [uc_cross]}
        if not do_full_sample:
            # encode (scaled latent)
            z_enc = sampler.stochastic_encode(
                z, torch.tensor([t_enc] * num_samples).to(model.device))
        else:
            z_enc = torch.randn_like(z)
        # decode it
        samples = sampler.decode(z_enc, cond, t_enc, unconditional_guidance_scale=scale,
                                 unconditional_conditioning=uc_full, callback=callback)
        x_samples_ddim = model.decode_first_stage(samples)
        result = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
        result = result.cpu().numpy().transpose(0, 2, 3, 1) * 255
    return [depth_image] + [Image.fromarray(img.astype(np.uint8)) for img in result]


def pad_image(input_image):
    pad_w, pad_h = np.max(((2, 2), np.ceil(
        np.array(input_image.size) / 64).astype(int)), axis=0) * 64 - input_image.size
    im_padded = Image.fromarray(
        np.pad(np.array(input_image), ((0, pad_h), (0, pad_w), (0, 0)), mode='edge'))
    return im_padded


def predict(input_image, prompt, steps, num_samples, scale, seed, eta, strength):
    init_image = input_image.convert("RGB")
    image = pad_image(init_image)  # resize to integer multiple of 32

    sampler.make_schedule(steps, ddim_eta=eta, verbose=True)
    assert 0. <= strength <= 1., 'can only work with strength in [0.0, 1.0]'
    do_full_sample = strength == 1.
    t_enc = min(int(strength * steps), steps-1)
    result = paint(
        sampler=sampler,
        image=image,
        prompt=prompt,
        t_enc=t_enc,
        seed=seed,
        scale=scale,
        num_samples=num_samples,
        callback=None,
        do_full_sample=do_full_sample
    )
    return result

model_path = os.path.join("weights", "512-depth-ema.ckpt")
sampler = initialize_model("configs/stable-diffusion/v2-midas-inference.yaml", model_path)

PROMPT = "a cool tree"
INPUT_IMAGE = ""
DDIM_STEPS = 100
GUIDANCE_SCALE = 7
STRENGTH = 0.55
SEED = 1
ETA = 0.0

result = predict(INPUT_IMAGE, PROMPT, DDIM_STEPS, 1, GUIDANCE_SCALE, SEED, ETA, STRENGTH)

result.save("test.png")

