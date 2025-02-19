import sys
import os
import torch
import numpy as np
import gradio as gr
from PIL import Image
from omegaconf import OmegaConf
from einops import repeat, rearrange
from pytorch_lightning import seed_everything
import pathlib
import cv2
import subprocess

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.data.util import AddMiDaS

torch.set_grad_enabled(False)

def vid2frames(video_path, frames_path, n=1, overwrite=True):      
    if not os.path.exists(frames_path) or overwrite: 
        try:
            for f in pathlib.Path(frames_path).glob('*.jpg'):
                f.unlink()
        except:
            pass
        assert os.path.exists(video_path), f"Video input {video_path} does not exist"
          
        vidcap = cv2.VideoCapture(video_path)
        success,image = vidcap.read()
        count = 0
        t=1
        success = True
        while success:
            if count % n == 0:
                image = cv2.resize(image, (1024, 576), interpolation = cv2.INTER_AREA)
                cv2.imwrite(frames_path + os.path.sep + f"{t:05}.jpg" , image)     # save frame as JPEG file
                t += 1
            success,image = vidcap.read()
            count += 1
        print("Converted %d frames" % count)
    else: print("Frames already unpacked")


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
    print(f"INPUT SIZE: {image.size}")

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

BATCH_NAME = "test"
PROMPT = "a colorful illustration, intricate, highly detailed, digital painting, artstation, concept art, art by artgerm and greg rutkowski and alphonse mucha and victo ngai"
VIDEO_INIT_PATH = "LP1.mp4"
# INPUT_IMAGE_PATH = os.path.join("assets", "rick.jpeg")
DDIM_STEPS = 100
GUIDANCE_SCALE = 7
STRENGTH = 0.55
SEED = 1
ETA = 0.0
OUTDIR = os.path.join(os.path.join("outputs", "depth2video"), BATCH_NAME)
SAVE_DEPTH_IMAGES = True


# video_in_frame_path = os.path.join(OUTDIR, 'inputframes') 
# os.makedirs(video_in_frame_path, exist_ok=True)
# print(f"Exporting Video Frames to {video_in_frame_path}...")
# vid2frames(VIDEO_INIT_PATH, video_in_frame_path, 1, True)

# model_path = os.path.join("weights", "512-depth-ema.ckpt")
# sampler = initialize_model("configs/stable-diffusion/v2-midas-inference.yaml", model_path)

# frame_num = 0
# for frame_path in sorted(os.listdir(video_in_frame_path)):
#     full_frame_path = os.path.join(video_in_frame_path, frame_path)
#     frame = Image.open(full_frame_path)
#     result = predict(frame, PROMPT, DDIM_STEPS, 1, GUIDANCE_SCALE, SEED, ETA, STRENGTH)
#     if SAVE_DEPTH_IMAGES:
#         save_depth_path = os.path.join(OUTDIR, f"depth_{frame_num:05d}.png")
#         result[0].save(save_frame_path) 
#     save_frame_path = os.path.join(OUTDIR, f"frame_{frame_num:05d}.png")
#     result[1].save(save_frame_path)
#     frame_num += 1

fps = 12
image_path = os.path.join(OUTDIR, "frame_%05d.png")
max_frames = 290
mp4_path = os.path.join(OUTDIR, "out.mp4")
cmd = [
    'ffmpeg',
    '-y',
    '-vcodec', 'png',
    '-r', str(fps),
    '-start_number', str(0),
    '-i', image_path,
    '-frames:v', str(max_frames),
    '-c:v', 'libx264',
    '-vf',
    f'fps={fps}',
    '-pix_fmt', 'yuv420p',
    '-crf', '17',
    '-preset', 'veryfast',
    '-pattern_type', 'sequence',
    mp4_path
]
process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
stdout, stderr = process.communicate()
if process.returncode != 0:
    print(stderr)
    raise RuntimeError(stderr)



