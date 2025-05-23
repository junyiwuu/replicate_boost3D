# Copyright 2023 Bingxin Ke, ETH Zurich and The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# --------------------------------------------------------------------------
# If you find this code useful, we kindly ask you to cite our paper in your work.
# Please find bibtex at: https://github.com/prs-eth/Marigold#-citation
# More information about the method can be found at https://marigoldmonodepth.github.io
# --------------------------------------------------------------------------


import math
from typing import Dict, Union

import matplotlib
import numpy as np
import torch
from PIL import Image
from scipy.optimize import minimize
from torch.utils.data import DataLoader, TensorDataset
from torchvision.transforms import Resize, InterpolationMode
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
from transformers import CLIPVisionModel, CLIPImageProcessor

from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    DiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.utils import BaseOutput, check_min_version

import pdb


class MaterialOutput(BaseOutput):
    albedo_np: np.ndarray
    albedo_pil: Image.Image
    uncertainty: Union[None, np.ndarray]

class MaterialPipeline(DiffusionPipeline):
    """
    Pipeline for monocular depth estimation using Marigold: https://marigoldmonodepth.github.io.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        unet (`UNet2DConditionModel`):
            Conditional U-Net to denoise the depth latent, conditioned on image latent.
        vae (`AutoencoderKL`):
            Variational Auto-Encoder (VAE) Model to encode and decode images and depth maps
            to and from latent representations.
        scheduler (`DDIMScheduler`):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents.
        text_encoder (`CLIPTextModel`):
            Text-encoder, for empty text embedding.
        tokenizer (`CLIPTokenizer`):
            CLIP tokenizer.
    """

    rgb_latent_scale_factor = 0.18215
    brdf_latent_scale_factor = 0.18215

    def __init__(
        self,
        unet: UNet2DConditionModel,
        vae_albedo: AutoencoderKL,
        vae_beauty: AutoencoderKL,
        scheduler: DDIMScheduler,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
    ):
        super().__init__()

        self.register_modules(
            unet=unet,
            vae_albedo=vae_albedo,
            vae_beauty=vae_beauty,
            scheduler=scheduler,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
        )

        self.empty_text_embed = None

    @torch.no_grad()
    def __call__(
        self,
        input_image: Image,
        denoising_steps: int = 10,
        ensemble_size: int = 10,
        processing_res: int = 512,
        match_input_res: bool = True,
        generator: Union[torch.Generator, None] = None,
        batch_size: int = 0,
        color_map: str = "Spectral",
        show_progress_bar: bool = True,
        resample_method: str = "bilinear",
        ensemble_kwargs: Dict = None,
    ) -> MaterialOutput:
        """
        Function invoked when calling the pipeline.

        Args:
            input_image (`Image`):
                Input RGB (or gray-scale) image.
            processing_res (`int`, *optional*, defaults to `768`):
                Maximum resolution of processing.
                If set to 0: will not resize at all.
            match_input_res (`bool`, *optional*, defaults to `True`):
                Resize depth prediction to match input resolution.
                Only valid if `limit_input_res` is not None.
            denoising_steps (`int`, *optional*, defaults to `10`):
                Number of diffusion denoising steps (DDIM) during inference.
            ensemble_size (`int`, *optional*, defaults to `10`):
                Number of predictions to be ensembled.
            batch_size (`int`, *optional*, defaults to `0`):
                Inference batch size, no bigger than `num_ensemble`.
                If set to 0, the script will automatically decide the proper batch size.
            show_progress_bar (`bool`, *optional*, defaults to `True`):
                Display a progress bar of diffusion denoising.
            ensemble_kwargs (`dict`, *optional*, defaults to `None`):
                Arguments for detailed ensembling settings.
        Returns:
            `MarigoldDepthOutput`: Output class for Marigold monocular depth prediction pipeline, including:
            - **depth_np** (`np.ndarray`) Predicted depth map, with depth values in the range of [0, 1]
            - **depth_colored** (`PIL.Image.Image`) Colorized depth map, with the shape of [3, H, W] and values in [0, 1]
            - **uncertainty** (`None` or `np.ndarray`) Uncalibrated uncertainty(MAD, median absolute deviation)
                    coming from ensembling. None if `ensemble_size = 1`
        """

        device = self.device
        input_size = input_image.size

        if not match_input_res:
            assert processing_res is not None, "Value error: `resize_output_back` is only valid with "
        assert processing_res >= 0
        assert denoising_steps >= 1
        assert ensemble_size >= 1

        # ----------------- Image Preprocess -----------------
        ######CHANGE
        # convert to torch.tensor if need
        if isinstance(input_image, Image.Image):
             # Resize image
            if processing_res > 0:
                input_image = self.resize_max_res(input_image, max_edge_resolution=processing_res)

            # Convert the image to RGB, to 1.remove the alpha channel 2.convert B&W to 3-channel
            input_image = input_image.convert("RGB")
            image = np.asarray(input_image)
            # Normalize rgb values
            rgb = np.transpose(image, (2, 0, 1))  # [H, W, rgb] -> [rgb, H, W]
            # rgb_norm = rgb / 255.0
            rgb_norm = rgb / 255.0 * 2.0 - 1.0  #  [0, 255] -> [-1, 1]
            rgb_norm = torch.from_numpy(rgb_norm).to(self.dtype)
            rgb_norm = rgb_norm.to(device)

        elif isinstance(input_image, torch.Tensor):
            rgb_norm = input_image.squeeze(0)
            rgb_norm = rgb_norm.to(self.device)
            assert rgb_norm.ndim==3, f"input_image dimesnion need to be 3, but got {rgb_norm.shape}"
        
        # assert rgb_norm.min() >= 0.0 and rgb_norm.max() <= 1.0
        assert rgb_norm.min() >= -1.0 and rgb_norm.max() <= 1.0

        # ----------------- Predicting albedo -----------------
        # Batch repeated input image
        duplicated_rgb = torch.stack([rgb_norm] * ensemble_size) # predict brdf for 10 times and ensemble them
        single_rgb_dataset = TensorDataset(duplicated_rgb)
        if batch_size > 0:
            _bs = batch_size
        else:
            _bs = self._find_batch_size(
                ensemble_size=ensemble_size,
                input_res=max(rgb_norm.shape[1:]),
                dtype=self.dtype,
            )

        single_rgb_loader = DataLoader(single_rgb_dataset, batch_size=_bs, shuffle=False)

        # Predict BRDFs
        brdf_pred_ls = []
        if show_progress_bar:
            iterable = tqdm(single_rgb_loader, desc=" " * 2 + "Inference batches", leave=False)
        else:
            iterable = single_rgb_loader
        
        for batch in iterable:
            (batched_img,) = batch
            brdf_pred_raw = self.single_infer(
                rgb_in=batched_img,
                num_inference_steps=denoising_steps,
                show_pbar=show_progress_bar,
            )
            brdf_pred_ls.append(brdf_pred_raw.detach().clone())
        brdf_preds = torch.concat(brdf_pred_ls, axis=0).squeeze()
        torch.cuda.empty_cache()  # clear vram cache for ensembling

        # ----------------- Test-time ensembling -----------------
        if ensemble_size > 1:
            # albedo_pred, rmo_pred = self._ensemble_brdfs(brdf_preds)
            albedo_pred = self._ensemble_brdfs(brdf_preds)
            uncert_pred = None
        elif ensemble_size == 1:
            # albedo_pred, rmo_pred = brdf_preds[:, 0:3, :, :], brdf_preds[:, 3:6, :, :]
            albedo_pred = brdf_preds
            uncert_pred = None
        else:
            raise Exception(['[INFO] Invalid ensemble_size.'])

        # Resize back to original resolution
        if match_input_res:
            rsz = Resize(input_size[::-1], interpolation=InterpolationMode.BICUBIC, antialias=True)
            albedo_pred = rsz(albedo_pred)
            # rmo_pred = rsz(rmo_pred)

        # transfer from tensor to numpy
        if len(albedo_pred.shape) == 4:
            albedo_pred = albedo_pred.squeeze(0).permute(1, 2, 0).cpu().numpy().astype(np.float32)
            # rmo_pred = rmo_pred.squeeze(0).permute(1, 2, 0).cpu().numpy().astype(np.float32)
        elif len(albedo_pred.shape) == 3:
            albedo_pred = albedo_pred.permute(1, 2, 0).cpu().numpy().astype(np.float32)
            # rmo_pred = rmo_pred.permute(1, 2, 0).cpu().numpy().astype(np.float32)
        else:
            raise Exception('[INFO] Invalid albedo or rmo shape.')

        # Colorize
        albedo_pred = np.nan_to_num(albedo_pred, nan=0.0)
        albedo_color = np.clip(albedo_pred + 1 / 2, 0, 1) # [-1,1]->[0,1]
        albedo_color = (albedo_color * 255).astype(np.uint8)
        albedo_color = Image.fromarray(albedo_color)

        return MaterialOutput(
            albedo_np=albedo_pred,
            albedo_pil=albedo_color,
            # rmo_np=rmo_pred,
            # rmo_pil=rmo_color,
            uncertainty=uncert_pred
        )

    def _encode_empty_text(self):
        """
        Encode text embedding for empty prompt.
        """
        prompt = ""
        text_inputs = self.tokenizer(
            prompt,
            padding="do_not_pad",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids.to(self.text_encoder.device)
        self.empty_text_embed = self.text_encoder(text_input_ids)[0].to(self.dtype)
        assert self.empty_text_embed.shape[-1] == 1024, f"_encode_empty_text :Text embedding dim mismatch! Got {self.empty_text_embed.shape[-1]}"
 

    @torch.no_grad()
    def single_infer(self, rgb_in: torch.Tensor, num_inference_steps: int, show_pbar: bool) -> torch.Tensor:
        """
        Perform an individual depth prediction without ensembling.
        Perform an individual BRDF prediction without ensembling.

        Args:
            rgb_in (`torch.Tensor`):
                Input RGB image.
            num_inference_steps (`int`):
                Number of diffusion denoisign steps (DDIM) during inference.
            show_pbar (`bool`):
                Display a progress bar of diffusion denoising.
        Returns:
            `torch.Tensor`: Predicted depth map.
        """

        device = rgb_in.device

        # Set timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps  # [T]

        # Encode image
        rgb_latent = self._encode_rgb(rgb_in)
        
        # Initial BRDF (noise)
        # brdf_latent_shape = [rgb_latent.shape[0], rgb_latent.shape[1] * 2, rgb_latent.shape[2], rgb_latent.shape[3]] # [b, 8, h, w]
        brdf_latent_shape = rgb_latent.shape # [b, 4, h, w]

        brdf_latent = torch.randn(brdf_latent_shape, device=device, dtype=self.dtype)
        # print("rgb_latent:", rgb_latent.device)
        # print("brdf_latent:", brdf_latent.device)


        # Batched empty text embedding
        if self.empty_text_embed is None:
            self._encode_empty_text()
        ####### add to(device) and to(self.dtype)
        ###### CHANGE
        batch_empty_text_embed = self.empty_text_embed.repeat((rgb_latent.shape[0], 1, 1)).to(device).to(self.dtype)  # [B, 2, 1024]

        # Denoising loop
        if show_pbar:
            iterable = tqdm(
                enumerate(timesteps),
                total=len(timesteps),
                leave=False,
                desc=" " * 4 + "Diffusion denoising",
            )
        else:
            iterable = enumerate(timesteps)

        for i, t in iterable:
            # unet_input = torch.cat([rgb_latent, normal_latent], dim=1)  # this order is important
            unet_input = torch.cat([brdf_latent, rgb_latent], dim=1)  # this order is important, [b,8,h,w]+[b,4,h,w]->[b,12,h,w]

            # predict the noise residual
            noise_pred = self.unet(unet_input, t, encoder_hidden_states=batch_empty_text_embed).sample  # [B, 4, h, w]

            # compute the previous noisy sample x_t -> x_t-1
            brdf_latent = self.scheduler.step(noise_pred, t, brdf_latent).prev_sample
        torch.cuda.empty_cache()
        brdf = self._decode_brdf(brdf_latent)

        # clip prediction
        brdf = torch.clip(input=brdf, min=-1.0, max=1.0)

        return brdf

    def _encode_rgb(self, rgb_in: torch.Tensor) -> torch.Tensor:
        """
        Encode RGB image into latent.

        Args:
            rgb_in (`torch.Tensor`):
                Input RGB image to be encoded.

        Returns:
            `torch.Tensor`: Image latent.
        """
        # encode
        assert rgb_in.ndim == 4, f"expect rgb_in dimension is 4, but got shape: {rgb_in.shape}"
        h = self.vae_beauty.encoder(rgb_in)
        
        moments = self.vae_beauty.quant_conv(h)
        mean, logvar = torch.chunk(moments, 2, dim=1)
        # scale latent, deterministic encoding
        rgb_latent = mean * self.rgb_latent_scale_factor
        return rgb_latent

    def _decode_brdf(self, brdf_latent: torch.Tensor) -> torch.Tensor:
        """
        Decode latent into BRDF

        Args:
            brdf_latent (`torch.Tensor`):
                brdf latent to be decoded.

        Returns:
            `torch.Tensor`: Decoded brdf map.
        """
        # scale latent
        brdf_latent = brdf_latent / self.brdf_latent_scale_factor
        # decode
        z = self.vae_albedo.post_quant_conv(brdf_latent[:, 0:4, :, :])
        albedo = self.vae_albedo.decoder(z)
        return albedo

    def _ensemble_brdfs(self, brdf_preds):
        brdf_pred = brdf_preds.mean(dim=0, keepdim=True) # [b,6,h,w]->[1,6,h,w]
        
        #return brdf_pred[:, 0:3, :, :], brdf_pred[:, 3:6, :, :] # [albedo, rmo]
        return brdf_pred

    @staticmethod
    def resize_max_res(img: Image.Image, max_edge_resolution: int) -> Image.Image:
        """
        Resize image to limit maximum edge length while keeping aspect ratio.

        Args:
            img (`Image.Image`):
                Image to be resized.
            max_edge_resolution (`int`):
                Maximum edge length (pixel).

        Returns:
            `Image.Image`: Resized image.
        """
        original_width, original_height = img.size
        downscale_factor = min(max_edge_resolution / original_width, max_edge_resolution / original_height)

        new_width = int(original_width * downscale_factor)
        new_height = int(original_height * downscale_factor)

        resized_img = img.resize((new_width, new_height))
        return resized_img

    @staticmethod
    def _find_batch_size(ensemble_size: int, input_res: int, dtype: torch.dtype) -> int:
        """
        Automatically search for suitable operating batch size.

        Args:
            ensemble_size (`int`):
                Number of predictions to be ensembled.
            input_res (`int`):
                Operating resolution of the input image.

        Returns:
            `int`: Operating batch size.
        """
        # Search table for suggested max. inference batch size
        bs_search_table = [
            # tested on A100-PCIE-80GB
            {"res": 768, "total_vram": 79, "bs": 35, "dtype": torch.float32},
            {"res": 1024, "total_vram": 79, "bs": 20, "dtype": torch.float32},
            # tested on A100-PCIE-40GB
            {"res": 768, "total_vram": 39, "bs": 15, "dtype": torch.float32},
            {"res": 1024, "total_vram": 39, "bs": 8, "dtype": torch.float32},
            {"res": 768, "total_vram": 39, "bs": 30, "dtype": torch.float16},
            {"res": 1024, "total_vram": 39, "bs": 15, "dtype": torch.float16},
            # tested on RTX3090, RTX4090
            {"res": 512, "total_vram": 23, "bs": 20, "dtype": torch.float32},
            {"res": 768, "total_vram": 23, "bs": 7, "dtype": torch.float32},
            {"res": 1024, "total_vram": 23, "bs": 3, "dtype": torch.float32},
            {"res": 512, "total_vram": 23, "bs": 40, "dtype": torch.float16},
            {"res": 768, "total_vram": 23, "bs": 18, "dtype": torch.float16},
            {"res": 1024, "total_vram": 23, "bs": 10, "dtype": torch.float16},
            # tested on GTX1080Ti
            {"res": 512, "total_vram": 10, "bs": 5, "dtype": torch.float32},
            {"res": 768, "total_vram": 10, "bs": 2, "dtype": torch.float32},
            {"res": 512, "total_vram": 10, "bs": 10, "dtype": torch.float16},
            {"res": 768, "total_vram": 10, "bs": 5, "dtype": torch.float16},
            {"res": 1024, "total_vram": 10, "bs": 3, "dtype": torch.float16},
        ]

        if not torch.cuda.is_available():
            return 1

        total_vram = torch.cuda.mem_get_info()[1] / 1024.0**3
        filtered_bs_search_table = [s for s in bs_search_table if s["dtype"] == dtype]
        for settings in sorted(
            filtered_bs_search_table,
            key=lambda k: (k["res"], -k["total_vram"]),
        ):
            if input_res <= settings["res"] and total_vram >= settings["total_vram"]:
                bs = settings["bs"]
                if bs > ensemble_size:
                    bs = ensemble_size
                elif bs > math.ceil(ensemble_size / 2) and bs < ensemble_size:
                    bs = math.ceil(ensemble_size / 2)
                return bs

        return 1