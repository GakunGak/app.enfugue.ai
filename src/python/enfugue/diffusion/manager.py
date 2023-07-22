from __future__ import annotations

import gc
import os
import PIL
import time
import torch
import random
import datetime
import traceback
import threading

from typing import Type, Union, Any, Optional, List, Tuple, Dict, Literal, Callable, TYPE_CHECKING
from hashlib import md5

from pibble.api.configuration import APIConfiguration
from pibble.api.exceptions import ConfigurationError
from pibble.util.files import dump_json

from enfugue.util import logger, check_download, check_make_directory
from enfugue.diffusion.constants import (
    DEFAULT_MODEL,
    DEFAULT_INPAINTING_MODEL,
    VAE_EMA,
    VAE_MSE,
    VAE_XL,
    CONTROLNET_CANNY,
    CONTROLNET_MLSD,
    CONTROLNET_HED,
    CONTROLNET_SCRIBBLE,
    CONTROLNET_TILE,
    CONTROLNET_INPAINT,
    CONTROLNET_DEPTH,
    CONTROLNET_NORMAL,
    CONTROLNET_POSE,
)

__all__ = ["DiffusionPipelineManager"]

if TYPE_CHECKING:
    from diffusers.models import ControlNetModel, AutoencoderKL
    from diffusers.schedulers.scheduling_utils import KarrasDiffusionSchedulers
    from enfugue.diffusion.upscale import Upscaler
    from enfugue.diffusion.pipeline import EnfugueStableDiffusionPipeline
    from enfugue.diffusion.edge.detect import EdgeDetector
    from enfugue.diffusion.depth.detect import DepthDetector
    from enfugue.diffusion.pose.detect import PoseDetector


def redact(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Redacts prompts from logs to encourage log sharing for troubleshooting.
    """
    return dict([(key, value if key not in ["prompt", "negative_prompt"] else "***") for key, value in kwargs.items()])


class KeepaliveThread(threading.Thread):
    """
    Calls the keepalive function every <n> seconds.
    """

    INTERVAL = 0.5
    KEEPALIVE_INTERVAL = 15

    def __init__(self, manager: DiffusionPipelineManager) -> None:
        super(KeepaliveThread, self).__init__()
        self.manager = manager
        self.stop_event = threading.Event()

    @property
    def stopped(self) -> bool:
        """
        Returns true IFF the stop event is set.
        """
        return self.stop_event.is_set()

    def stop(self) -> None:
        """
        Stops the thread.
        """
        self.stop_event.set()

    def run(self) -> None:
        """
        The threading run loop.
        """
        last_keepalive = datetime.datetime.now()
        while not self.stopped:
            time.sleep(self.INTERVAL)
            now = datetime.datetime.now()
            if (now - last_keepalive).total_seconds() > self.KEEPALIVE_INTERVAL:
                callback = self.manager.keepalive_callback
                logger.debug(f"Pipeline still initializing. Please wait.")
                callback()
                last_keepalive = now


class DiffusionPipelineManager:
    TENSORRT_STAGES = ["unet"]  # TODO: Get others to work with multidiff (clip works but isnt worth it right now)
    TENSORRT_ALWAYS_USE_CONTROLLED_UNET = False  # TODO: Figure out if this is possible

    DEFAULT_CHUNK = 64
    DEFAULT_SIZE = 512

    _keepalive_thread: KeepaliveThread
    _keepalive_callback: Callable[[], None]
    _scheduler: KarrasDiffusionSchedulers
    _multi_scheduler: KarrasDiffusionSchedulers
    _pipeline: EnfugueStableDiffusionPipeline
    _refiner_pipeline: EnfugueStableDiffusionPipeline
    _inpainter_pipeline: EnfugueStableDiffusionPipeline
    _refiner_size: int
    _inpainter_size: int

    def __init__(self, configuration: Optional[APIConfiguration] = None) -> None:
        self.configuration = APIConfiguration()
        if configuration:
            self.configuration = configuration

    @property
    def safe(self) -> bool:
        """
        Returns true if safety checking should be enabled.
        """
        if not hasattr(self, "_safe"):
            self._safe = self.configuration.get("enfugue.safe", True)
        return self._safe

    @safe.setter
    def safe(self, val: bool) -> None:
        """
        Sets a new value for safety checking. Destroys the pipeline.
        """
        if val != getattr(self, "_safe", None):
            self._safe = val
            self.unload_pipeline("safety checking enabled or disabled")

    @property
    def device(self) -> torch.device:
        """
        Gets the device that will be executed on
        """
        if not hasattr(self, "_device"):
            from enfugue.diffusion.util import get_optimal_device

            self._device = get_optimal_device()
        return self._device

    @device.setter
    def device(self, new_device: Optional[Literal["cpu", "cuda", "dml", "mps"]]) -> None:
        """
        Changes the device.
        """
        if new_device is None:
            from enfugue.diffusion.util import get_optimal_device

            device = get_optimal_device()
        elif new_device == "dml":
            import torch_directml

            device = torch_directml.device()
        else:
            import torch

            device = torch.device(new_device)
        self._device = device

    def clear_memory(self) -> None:
        """
        Clears cached data
        """
        if self.device.type == "cuda":
            import torch
            import torch.cuda

            torch.cuda.empty_cache()
        elif self.device.type == "mps":
            import torch
            import torch.mps

            torch.mps.empty_cache()
        gc.collect()

    @property
    def seed(self) -> int:
        """
        Gets the seed. If there is none, creates a random one once.
        """
        if not hasattr(self, "_seed"):
            self._seed = self.configuration.get("enfugue.seed", random.randint(0, 2**63 - 1))
        return self._seed

    @seed.setter
    def seed(self, new_seed: int) -> None:
        """
        Re-seeds the pipeline. This deletes the generator so it gets re-initialized.
        """
        self._seed = new_seed
        del self.generator

    @property
    def keepalive_callback(self) -> Callable[[], None]:
        """
        A callback function to call during long operations.
        """
        if not hasattr(self, "_keepalive_callback"):
            return lambda: None
        return self._keepalive_callback

    @keepalive_callback.setter
    def keepalive_callback(self, new_callback: Callable[[], None]) -> None:
        """
        Sets the callback
        """
        if hasattr(self, "_keepalive_callback") and self._keepalive_callback is not new_callback:
            logger.debug(f"Setting keepalive callback to {new_callback}")
        self._keepalive_callback = new_callback

    @keepalive_callback.deleter
    def keepalive_callback(self) -> None:
        """
        Removes the callback.
        """
        if hasattr(self, "_keepalive_callback"):
            del self._keepalive_callback

    def start_keepalive(self) -> None:
        """
        Starts a thread which will call the keepalive callback every <n> seconds.
        """
        if not hasattr(self, "_keepalive_thread") or not self._keepalive_thread.is_alive():
            keepalive_callback = self.keepalive_callback
            self._keepalive_thread = KeepaliveThread(self)
            self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        """
        Stops the thread which calls the keepalive.
        """
        if hasattr(self, "_keepalive_thread"):
            if self._keepalive_thread.is_alive():
                self._keepalive_thread.stop()
                self._keepalive_thread.join()
            del self._keepalive_thread

    @property
    def generator(self) -> torch.Generator:
        """
        Creates the generator once, otherwise returns it.
        """
        if not hasattr(self, "_generator"):
            try:
                self._generator = torch.Generator(device=self.device)
            except RuntimeError:
                # Unsupported device, go to CPU
                self._generator = torch.Generator()
            self._generator.manual_seed(self.seed)
        return self._generator

    @generator.deleter
    def generator(self) -> None:
        """
        Removes an existing generator.
        """
        if hasattr(self, "_generator"):
            delattr(self, "_generator")

    def get_scheduler_class(
        self,
        scheduler: Optional[
            Literal[
                "ddim",
                "ddpm",
                "deis",
                "dpmsm",
                "dpmss",
                "heun",
                "dpmd",
                "adpmd",
                "dpmsde",
                "unipc",
                "lmsd",
                "pndm",
                "eds",
                "eads",
            ]
        ],
    ) -> KarrasDiffusionSchedulers:
        """
        Sets the scheduler class
        """
        if not scheduler:
            return None
        elif scheduler == "ddim":
            from diffusers.schedulers import DDIMScheduler

            return DDIMScheduler
        elif scheduler == "ddpm":
            from diffusers.schedulers import DDPMScheduler

            return DDPMScheduler
        elif scheduler == "deis":
            from diffusers.schedulers import DEISMultistepScheduler

            return DEISMultistepScheduler
        elif scheduler == "dpmsm":
            from diffusers.schedulers import DPMSolverMultistepScheduler

            return DPMSolverMultistepScheduler
        elif scheduler == "dpmss":
            from diffusers.schedulers import DPMSolverSinglestepScheduler

            return DPMSolverSinglestepScheduler
        elif scheduler == "heun":
            from diffusers.schedulers import HeunDiscreteScheduler

            return HeunDiscreteScheduler
        elif scheduler == "dpmd":
            from diffusers.schedulers import KDPM2DiscreteScheduler

            return KDPM2DiscreteScheduler
        elif scheduler == "adpmd":
            from diffusers.schedulers import KDPM2AncestralDiscreteScheduler

            return KDPM2AncestralDiscreteScheduler
        elif scheduler == "dpmsde":
            from diffusers.schedulers import DPMSolverSDEScheduler

            return DPMSolverSDEScheduler
        elif scheduler == "unipc":
            from diffusers.schedulers import UniPCMultistepScheduler

            return UniPCMultistepScheduler
        elif scheduler == "lmsd":
            from diffusers.schedulers import LMSDiscreteScheduler

            return LMSDiscreteScheduler
        elif scheduler == "pndm":
            from diffusers.schedulers import PNDMScheduler

            return PNDMScheduler
        elif scheduler == "eds":
            from diffusers.schedulers import EulerDiscreteScheduler

            return EulerDiscreteScheduler
        elif scheduler == "eads":
            from diffusers.schedulers import EulerAncestralDiscreteScheduler

            return EulerAncestralDiscreteScheduler
        raise ValueError(f"Unknown scheduler {scheduler}")

    @property
    def scheduler(self) -> Optional[KarrasDiffusionSchedulers]:
        """
        Gets the scheduler class to instantiate.
        """
        if not hasattr(self, "_scheduler"):
            return None
        return self._scheduler

    @scheduler.setter
    def scheduler(
        self,
        new_scheduler: Optional[
            Literal[
                "ddim",
                "ddpm",
                "deis",
                "dpmsm",
                "dpmss",
                "heun",
                "dpmd",
                "adpmd",
                "dpmsde",
                "unipc",
                "lmsd",
                "pndm",
                "eds",
                "eads",
            ]
        ],
    ) -> None:
        """
        Sets the scheduler class
        """
        if not new_scheduler:
            if hasattr(self, "_scheduler"):
                delattr(self, "_scheduler")
                self.unload_pipeline("returning to default scheduler")
            return
        scheduler_class = self.get_scheduler_class(new_scheduler)
        if not hasattr(self, "_scheduler") or self._scheduler is not scheduler_class:
            logger.debug(f"Changing to scheduler {scheduler_class.__name__} ({new_scheduler})")
            self._scheduler = scheduler_class
        if hasattr(self, "_pipeline"):
            logger.debug(f"Hot-swapping pipeline scheduler.")
            self._pipeline.scheduler = self.scheduler.from_config(self._pipeline.scheduler_config)  # type: ignore
        if hasattr(self, "_inpainter_pipeline"):
            logger.debug(f"Hot-swapping inpainter pipeline scheduler.")
            self._inpainter_pipeline.scheduler = self.scheduler.from_config(self._inpainter_pipeline.scheduler_config)  # type: ignore
        if hasattr(self, "_refiner_pipeline"):
            logger.debug(f"Hot-swapping refiner pipeline scheduler.")
            self._refiner_pipeline.scheduler = self.scheduler.from_config(self._refiner_pipeline.scheduler_config)  # type: ignore

    @property
    def multi_scheduler(self) -> Optional[KarrasDiffusionSchedulers]:
        """
        Gets the multi-diffusion scheduler class to instantiate.
        """
        if not hasattr(self, "_multi_scheduler"):
            return None
        return self._multi_scheduler

    @multi_scheduler.setter
    def multi_scheduler(
        self, new_multi_scheduler: Optional[Literal["ddim", "ddpm", "deis", "dpmsm", "dpmss", "eds", "eads"]]
    ) -> None:
        """
        Sets the multi_scheduler class
        """
        if not new_multi_scheduler:
            if hasattr(self, "_multi_scheduler"):
                delattr(self, "_multi_scheduler")
                self.unload_pipeline("returning to default multi_scheduler")
            return
        multi_scheduler_class = self.get_scheduler_class(new_multi_scheduler)
        if not hasattr(self, "_multi_scheduler") or self._multi_scheduler is not multi_scheduler_class:
            logger.debug(f"Changing to multi_scheduler {multi_scheduler_class.__name__} ({new_multi_scheduler})")
            self._multi_scheduler = multi_scheduler_class
        if hasattr(self, "_pipeline"):
            logger.debug(f"Hot-swapping pipeline multi_scheduler.")
            self._pipeline.multi_scheduler = self.multi_scheduler.from_config(self._pipeline.multi_scheduler_config)  # type: ignore
        if hasattr(self, "_inpainter_pipeline"):
            logger.debug(f"Hot-swapping inpainter pipeline multi_scheduler.")
            self._inpainter_pipeline.multi_scheduler = self.multi_scheduler.from_config(self._inpainter_pipeline.multi_scheduler_config)  # type: ignore
        if hasattr(self, "_refiner_pipeline"):
            logger.debug(f"Hot-swapping refiner pipeline multi_scheduler.")
            self._refiner_pipeline.multi_scheduler = self.multi_scheduler.from_config(self._refiner_pipeline.multi_scheduler_config)  # type: ignore

    def get_vae(self, vae: Optional[str] = None) -> Optional[AutoencoderKL]:
        """
        Loads the VAE
        """
        if vae is None:
            return None
        from diffusers.models import AutoencoderKL

        expected_vae_location = os.path.join(self.engine_cache_dir, "models--" + vae.replace("/", "--"))

        if not os.path.exists(expected_vae_location):
            logger.info(f"VAE {vae} does not exist in cache directory {self.engine_cache_dir}, it will be downloaded.")

        result = AutoencoderKL.from_pretrained(
            vae,
            torch_dtype=self.dtype,
            cache_dir=self.engine_cache_dir,
        )
        return result.to(device=self.device)

    @property
    def vae(self) -> Optional[AutoencoderKL]:
        """
        Gets the configured VAE (or none.)
        """
        if not hasattr(self, "_vae"):
            self._vae = self.get_vae(self.vae_name)
        return self._vae

    @vae.setter
    def vae(
        self,
        new_vae: Optional[Literal["ema", "mse", "xl"]],
    ) -> None:
        """
        Sets a new vae.
        """
        pretrained_path = None
        if new_vae == "ema":
            pretrained_path = VAE_EMA
        elif new_vae == "mse":
            pretrained_path = VAE_MSE
        elif new_vae == "xl":
            pretrained_path = VAE_XL

        if pretrained_path is None and new_vae:
            logger.error(f"Unsupported VAE {new_vae}")

        existing_vae = getattr(self, "_vae", None)

        if (
            (not existing_vae and new_vae)
            or (existing_vae and not new_vae)
            or (existing_vae and new_vae and self.vae_name != new_vae)
        ):
            if not new_vae:
                self._vae_name = None  # type: ignore
                self._vae = None

                self.unload_pipeline("VAE resetting to default")
                self.unload_refiner("VAE resetting to default")
                self.unload_inpainter("VAE resetting to default")
            else:
                self._vae_name = new_vae
                self._vae = self.get_vae(pretrained_path)
                if self.tensorrt_is_ready and "vae" in self.TENSORRT_STAGES:
                    self.unload_pipeline("VAE changing")
                elif hasattr(self, "_pipeline"):
                    logger.debug(f"Hot-swapping pipeline VAE to {new_vae}")
                    self._pipeline.vae = self._vae
                if self.refiner_tensorrt_is_ready and "vae" in self.TENSORRT_STAGES:
                    self.unload_refiner("VAE changing")
                elif hasattr(self, "_refiner_pipeline"):
                    logger.debug(f"Hot-swapping refiner pipeline VAE to {new_vae}")
                    self._refiner_pipeline.vae = self._vae
                if self.inpainter_tensorrt_is_ready and "vae" in self.TENSORRT_STAGES:
                    self.unload_inpainter("VAE changing")
                elif hasattr(self, "_inpainter_pipeline"):
                    logger.debug(f"Hot-swapping inpainter pipeline VAE to {new_vae}")
                    self._inpainter_pipeline.vae = self._vae

    @property
    def vae_name(self) -> Optional[str]:
        """
        Gets the name of the VAE, if one was set.
        """
        if not hasattr(self, "_vae_name"):
            self._vae_name = self.configuration.get("enfugue.vae", None)
        return self._vae_name

    @property
    def size(self) -> int:
        """
        Gets the base engine size in pixels when chunking (default always.)
        """
        if not hasattr(self, "_size"):
            self._size = int(self.configuration.get("enfugue.size", DiffusionPipelineManager.DEFAULT_SIZE))
        return self._size

    @size.setter
    def size(self, new_size: int) -> None:
        """
        Sets the base engine size in pixels.
        """
        if hasattr(self, "_size") and self._size != new_size:
            if self.tensorrt_is_ready:
                self.unload_pipeline("engine size changing")
            elif hasattr(self, "_pipeline"):
                logger.debug("Setting pipeline engine size in-place.")
                self._pipeline.engine_size = new_size
        self._size = new_size

    @property
    def refiner_size(self) -> int:
        """
        Gets the refiner engine size in pixels when chunking (default always.)
        """
        if not hasattr(self, "_refiner_size"):
            if self.is_sdxl and not self.refiner_is_sdxl:
                return 512
            elif not self.is_sdxl and self.refiner_is_sdxl:
                return 1024
            return self.size
        return self._refiner_size

    @refiner_size.setter
    def refiner_size(self, new_refiner_size: Optional[int]) -> None:
        """
        Sets the refiner engine size in pixels.
        """
        if new_refiner_size is None:
            if hasattr(self, "_refiner_size"):
                if self._refiner_size != self.size and self.refiner_tensorrt_is_ready:
                    self.unload_refiner("engine size changing")
                elif hasattr(self, "_refiner_pipeline"):
                    logger.debug("Setting refiner engine size in-place.")
                    self._refiner_pipeline.engine_size = self.size
                delattr(self, "_refiner_size")
        elif hasattr(self, "_refiner_size") and self._refiner_size != new_refiner_size:
            if self.refiner_tensorrt_is_ready:
                self.unload_refiner("engine size changing")
            elif hasattr(self, "_refiner_pipeline"):
                logger.debug("Setting refiner engine size in-place.")
                self._refiner_pipeline.engine_size = new_refiner_size
        if new_refiner_size is not None:
            self._refiner_size = new_refiner_size

    @property
    def inpainter_size(self) -> int:
        """
        Gets the inpainter engine size in pixels when chunking (default always.)
        """
        if not hasattr(self, "_inpainter_size"):
            if self.is_sdxl and not self.inpainter_is_sdxl:
                return 512
            elif not self.is_sdxl and self.inpainter_is_sdxl:
                return 1024
            return self.size
        return self._inpainter_size

    @inpainter_size.setter
    def inpainter_size(self, new_inpainter_size: Optional[int]) -> None:
        """
        Sets the inpainter engine size in pixels.
        """
        if new_inpainter_size is None:
            if hasattr(self, "_inpainter_size"):
                if self._inpainter_size != self.size and self.inpainter_tensorrt_is_ready:
                    self.unload_inpainter("engine size changing")
                elif hasattr(self, "_inpainter_pipeline"):
                    logger.debug("Setting inpainter engine size in-place.")
                    self._inpainter_pipeline.engine_size = self.size
                delattr(self, "_inpainter_size")
        elif hasattr(self, "_inpainter_size") and self._inpainter_size != new_inpainter_size:
            if self.inpainter_tensorrt_is_ready:
                self.unload_inpainter("engine size changing")
            elif hasattr(self, "_inpainter_pipeline"):
                logger.debug("Setting inpainter engine size in-place.")
                self._inpainter_pipeline.engine_size = new_inpainter_size
        if new_inpainter_size is not None:
            self._inpainter_size = new_inpainter_size

    @property
    def chunking_size(self) -> int:
        """
        Gets the chunking size in pixels.
        """
        if not hasattr(self, "_chunking_size"):
            self._chunking_size = int(
                self.configuration.get("enfugue.chunk.size", DiffusionPipelineManager.DEFAULT_CHUNK)
            )
        return self._chunking_size

    @chunking_size.setter
    def chunking_size(self, new_chunking_size: int) -> None:
        """
        Sets the new chunking size. This doesn't require a restart.
        """
        self._chunking_size = new_chunking_size

    @property
    def chunking_blur(self) -> int:
        """
        Gets the chunking blur in pixels.
        """
        if not hasattr(self, "_chunking_blur"):
            self._chunking_blur = int(
                self.configuration.get("enfugue.chunk.blur", DiffusionPipelineManager.DEFAULT_CHUNK)
            )
        return self._chunking_blur

    @chunking_blur.setter
    def chunking_blur(self, new_chunking_blur: int) -> None:
        """
        Sets the new chunking blur. This doesn't require a restart.
        """
        self._chunking_blur = new_chunking_blur

    @property
    def engine_root(self) -> str:
        """
        Gets the root of the engine.
        """
        path = self.configuration.get("enfugue.engine.root", "~/.cache/enfugue")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_cache_dir(self) -> str:
        """
        Gets the cache for diffusers-downloaded configuration files, base models, etc.
        """
        path = self.configuration.get("enfugue.engine.cache", "~/.cache/enfugue/cache")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_checkpoints_dir(self) -> str:
        """
        Gets where checkpoints are downloaded in.
        """
        path = self.configuration.get("enfugue.engine.checkpoint", "~/.cache/enfugue/checkpoint")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_other_dir(self) -> str:
        """
        Gets where any other weights are download in
        """
        path = self.configuration.get("enfugue.engine.other", "~/.cache/enfugue/other")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_lora_dir(self) -> str:
        """
        Gets where lora are downloaded in.
        """
        path = self.configuration.get("enfugue.engine.lora", "~/.cache/enfugue/lora")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_lycoris_dir(self) -> str:
        """
        Gets where lycoris are downloaded in.
        """
        path = self.configuration.get("enfugue.engine.lycoris", "~/.cache/enfugue/lycoris")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_inversion_dir(self) -> str:
        """
        Gets where inversion are downloaded to.
        """
        path = self.configuration.get("enfugue.engine.inversion", "~/.cache/enfugue/inversion")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def engine_tensorrt_dir(self) -> str:
        """
        Gets where TensorRT engines are built.
        """
        path = self.configuration.get("enfugue.engine.tensorrt", "~/.cache/enfugue/tensorrt")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def model_tensorrt_dir(self) -> str:
        """
        Gets where tensorrt engines will be built per model.
        """
        path = os.path.join(self.engine_tensorrt_dir, self.model_name)
        check_make_directory(path)
        return path

    @property
    def refiner_tensorrt_dir(self) -> str:
        """
        Gets where tensorrt engines will be built per refiner.
        """
        if not self.refiner_name:
            raise ValueError("No refiner set")
        path = os.path.join(self.engine_tensorrt_dir, self.refiner_name)
        check_make_directory(path)
        return path

    @property
    def inpainter_tensorrt_dir(self) -> str:
        """
        Gets where tensorrt engines will be built per inpainter.
        """
        if not self.inpainter_name:
            raise ValueError("No inpainter set")
        path = os.path.join(self.engine_tensorrt_dir, self.inpainter_name)
        check_make_directory(path)
        return path

    @property
    def engine_diffusers_dir(self) -> str:
        """
        Gets where diffusers caches are saved.
        """
        path = self.configuration.get("enfugue.engine.diffusers", "~/.cache/enfugue/diffusers")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def model_diffusers_dir(self) -> str:
        """
        Gets where the diffusers cache will be for the current model.
        """
        path = os.path.join(self.engine_diffusers_dir, self.model_name)
        check_make_directory(path)
        return path

    @property
    def refiner_diffusers_dir(self) -> str:
        """
        Gets where the diffusers cache will be for the current refiner.
        """
        if not self.refiner_name:
            raise ValueError("No refiner set")
        path = os.path.join(self.engine_diffusers_dir, self.refiner_name)
        check_make_directory(path)
        return path

    @property
    def inpainter_diffusers_dir(self) -> str:
        """
        Gets where the diffusers cache will be for the current inpainter.
        """
        if not self.inpainter_name:
            raise ValueError("No inpainter set")
        path = os.path.join(self.engine_diffusers_dir, self.inpainter_name)
        check_make_directory(path)
        return path

    @property
    def engine_onnx_dir(self) -> str:
        """
        Gets where ONNX models are built (when using DirectML)
        """
        path = self.configuration.get("enfugue.engine.onnx", "~/.cache/enfugue/onnx")
        if path.startswith("~"):
            path = os.path.expanduser(path)
        path = os.path.realpath(path)
        check_make_directory(path)
        return path

    @property
    def model_onnx_dir(self) -> str:
        """
        Gets where the onnx cache will be for the current model.
        """
        path = os.path.join(self.engine_onnx_dir, self.model_name)
        check_make_directory(path)
        return path

    @property
    def refiner_onnx_dir(self) -> str:
        """
        Gets where the onnx cache will be for the current refiner.
        """
        if not self.refiner_name:
            raise ValueError("No refiner set")
        path = os.path.join(self.engine_onnx_dir, self.refiner_name)
        check_make_directory(path)
        return path

    @property
    def inpainter_onnx_dir(self) -> str:
        """
        Gets where the onnx cache will be for the current inpainter.
        """
        if not self.inpainter_name:
            raise ValueError("No inpainter set")
        path = os.path.join(self.engine_onnx_dir, self.inpainter_name)
        check_make_directory(path)
        return path

    @staticmethod
    def get_clip_key(
        size: int, lora: List[Tuple[str, float]], lycoris: List[Tuple[str, float]], inversion: List[str], **kwargs: Any
    ) -> str:
        """
        Uses hashlib to generate the unique key for the CLIP engine.
        CLIP must be rebuilt for each:
            1. Model
            2. Dimension
            3. LoRA
            4. LyCORIS
            5. Textual Inversion
        """
        return md5(
            "-".join(
                [
                    str(size),
                    ":".join(
                        "=".join([str(part) for part in lora_weight])
                        for lora_weight in sorted(lora, key=lambda lora_part: lora_part[0])
                    ),
                    ":".join(
                        "=".join([str(part) for part in lycoris_weight])
                        for lycoris_weight in sorted(lycoris, key=lambda lycoris_part: lycoris_part[0])
                    ),
                    ":".join(sorted(inversion)),
                ]
            ).encode("utf-8")
        ).hexdigest()

    @property
    def model_clip_key(self) -> str:
        """
        Gets the CLIP key for the current configuration.
        """
        return DiffusionPipelineManager.get_clip_key(
            size=self.size,
            lora=self.lora_names_weights,
            lycoris=self.lycoris_names_weights,
            inversion=self.inversion_names,
        )

    @property
    def model_tensorrt_clip_dir(self) -> str:
        """
        Gets where the tensorrt CLIP engine will be stored.
        """
        path = os.path.join(self.model_tensorrt_dir, "clip", self.model_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def model_onnx_clip_dir(self) -> str:
        """
        Gets where the onnx CLIP engine will be stored.
        """
        path = os.path.join(self.model_onnx_dir, "clip", self.model_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_clip_key(self) -> str:
        """
        Gets the CLIP key for the current configuration.
        """
        return DiffusionPipelineManager.get_clip_key(size=self.size, lora=[], lycoris=[], inversion=[])

    @property
    def refiner_tensorrt_clip_dir(self) -> str:
        """
        Gets where the tensorrt CLIP engine will be stored.
        """
        path = os.path.join(self.refiner_tensorrt_dir, "clip", self.refiner_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_onnx_clip_dir(self) -> str:
        """
        Gets where the onnx CLIP engine will be stored.
        """
        path = os.path.join(self.refiner_onnx_dir, "clip", self.refiner_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_clip_key(self) -> str:
        """
        Gets the CLIP key for the current configuration.
        """
        return DiffusionPipelineManager.get_clip_key(size=self.size, lora=[], lycoris=[], inversion=[])

    @property
    def inpainter_tensorrt_clip_dir(self) -> str:
        """
        Gets where the tensorrt CLIP engine will be stored.
        """
        path = os.path.join(self.inpainter_tensorrt_dir, "clip", self.inpainter_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_onnx_clip_dir(self) -> str:
        """
        Gets where the onnx CLIP engine will be stored.
        """
        path = os.path.join(self.inpainter_onnx_dir, "clip", self.inpainter_clip_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @staticmethod
    def get_unet_key(
        size: int,
        lora: List[Tuple[str, float]],
        lycoris: List[Tuple[str, float]],
        inversion: List[str],
        **kwargs: Any,
    ) -> str:
        """
        Uses hashlib to generate the unique key for the UNET engine.
        UNET must be rebuilt for each:
            1. Model
            2. Dimension
            3. LoRA
            4. LyCORIS
            5. Textual Inversion
        """
        return md5(
            "-".join(
                [
                    str(size),
                    ":".join(
                        "=".join([str(part) for part in lora_weight])
                        for lora_weight in sorted(lora, key=lambda lora_part: lora_part[0])
                    ),
                    ":".join(
                        "=".join([str(part) for part in lycoris_weight])
                        for lycoris_weight in sorted(lycoris, key=lambda lycoris_part: lycoris_part[0])
                    ),
                    ":".join(sorted(inversion)),
                ]
            ).encode("utf-8")
        ).hexdigest()

    @property
    def model_unet_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_unet_key(
            size=self.size,
            lora=self.lora_names_weights,
            lycoris=self.lycoris_names_weights,
            inversion=self.inversion_names,
        )

    @property
    def model_tensorrt_unet_dir(self) -> str:
        """
        Gets where the tensorrt UNET engine will be stored.
        """
        path = os.path.join(self.model_tensorrt_dir, "unet", self.model_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def model_onnx_unet_dir(self) -> str:
        """
        Gets where the onnx UNET engine will be stored.
        """
        path = os.path.join(self.model_onnx_dir, "unet", self.model_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_unet_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_unet_key(size=self.size, lora=[], lycoris=[], inversion=[])

    @property
    def refiner_tensorrt_unet_dir(self) -> str:
        """
        Gets where the tensorrt UNET engine will be stored for the refiner.
        """
        path = os.path.join(self.refiner_tensorrt_dir, "unet", self.refiner_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_onnx_unet_dir(self) -> str:
        """
        Gets where the onnx UNET engine will be stored for the refiner.
        """
        path = os.path.join(self.refiner_onnx_dir, "unet", self.refiner_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_unet_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_unet_key(size=self.size, lora=[], lycoris=[], inversion=[])

    @property
    def inpainter_tensorrt_unet_dir(self) -> str:
        """
        Gets where the tensorrt UNET engine will be stored for the inpainter.
        """
        path = os.path.join(self.inpainter_tensorrt_dir, "unet", self.inpainter_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_onnx_unet_dir(self) -> str:
        """
        Gets where the onnx UNET engine will be stored for the inpainter.
        """
        path = os.path.join(self.inpainter_onnx_dir, "unet", self.inpainter_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @staticmethod
    def get_controlled_unet_key(
        size: int,
        lora: List[Tuple[str, float]],
        lycoris: List[Tuple[str, float]],
        inversion: List[str],
        **kwargs: Any,
    ) -> str:
        """
        Uses hashlib to generate the unique key for the UNET engine with controlnet blocks.
        ControlledUNET must be rebuilt for each:
            1. Model
            2. Dimension
            3. LoRA
            4. LyCORIS
            5. Textual Inversion
        """
        return md5(
            "-".join(
                [
                    str(size),
                    ":".join(
                        "=".join([str(part) for part in lora_weight])
                        for lora_weight in sorted(lora, key=lambda lora_part: lora_part[0])
                    ),
                    ":".join(
                        "=".join([str(part) for part in lycoris_weight])
                        for lycoris_weight in sorted(lycoris, key=lambda lycoris_part: lycoris_part[0])
                    ),
                    ":".join(sorted(inversion)),
                ]
            ).encode("utf-8")
        ).hexdigest()

    @property
    def model_controlled_unet_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_controlled_unet_key(
            size=self.size,
            lora=self.lora_names_weights,
            lycoris=self.lycoris_names_weights,
            inversion=self.inversion_names,
        )

    @property
    def model_tensorrt_controlled_unet_dir(self) -> str:
        """
        Gets where the tensorrt Controlled UNet engine will be stored.
        """
        path = os.path.join(self.model_tensorrt_dir, "controlledunet", self.model_controlled_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def model_onnx_controlled_unet_dir(self) -> str:
        """
        Gets where the onnx Controlled UNet engine will be stored.
        """
        path = os.path.join(self.model_onnx_dir, "controlledunet", self.model_controlled_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_controlled_unet_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_controlled_unet_key(size=self.size, lora=[], lycoris=[], inversion=[])

    @property
    def refiner_tensorrt_controlled_unet_dir(self) -> str:
        """
        Gets where the tensorrt Controlled UNet engine will be stored for the refiner.
        TODO: determine if this should exist.
        """
        path = os.path.join(self.refiner_tensorrt_dir, "controlledunet", self.refiner_controlled_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_onnx_controlled_unet_dir(self) -> str:
        """
        Gets where the onnx Controlled UNet engine will be stored for the refiner.
        TODO: determine if this should exist.
        """
        path = os.path.join(self.refiner_onnx_dir, "controlledunet", self.refiner_controlled_unet_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @staticmethod
    def get_vae_key(size: int, **kwargs: Any) -> str:
        """
        Uses hashlib to generate the unique key for the VAE engine.
        VAE must be rebuilt for each:
            1. Model
            2. Dimension
        """
        return md5(str(size).encode("utf-8")).hexdigest()

    @property
    def model_vae_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_vae_key(size=self.size)

    @property
    def model_tensorrt_vae_dir(self) -> str:
        """
        Gets where the tensorrt VAE engine will be stored.
        """
        path = os.path.join(self.model_tensorrt_dir, "vae", self.model_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def model_onnx_vae_dir(self) -> str:
        """
        Gets where the onnx VAE engine will be stored.
        """
        path = os.path.join(self.model_onnx_dir, "vae", self.model_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_vae_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_vae_key(size=self.size)

    @property
    def refiner_tensorrt_vae_dir(self) -> str:
        """
        Gets where the tensorrt VAE engine will be stored for the refiner.
        """
        path = os.path.join(self.refiner_tensorrt_dir, "vae", self.refiner_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def refiner_onnx_vae_dir(self) -> str:
        """
        Gets where the onnx VAE engine will be stored for the refiner.
        """
        path = os.path.join(self.refiner_onnx_dir, "vae", self.refiner_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_vae_key(self) -> str:
        """
        Gets the UNET key for the current configuration.
        """
        return DiffusionPipelineManager.get_vae_key(size=self.size)

    @property
    def inpainter_tensorrt_vae_dir(self) -> str:
        """
        Gets where the tensorrt VAE engine will be stored for the inpainter.
        """
        path = os.path.join(self.inpainter_tensorrt_dir, "vae", self.inpainter_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def inpainter_onnx_vae_dir(self) -> str:
        """
        Gets where the onnx VAE engine will be stored for the inpainter.
        """
        path = os.path.join(self.inpainter_onnx_dir, "vae", self.inpainter_vae_key)
        check_make_directory(path)
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.exists(metadata_path):
            self.write_model_metadata(metadata_path)
        return path

    @property
    def tensorrt_is_supported(self) -> bool:
        """
        Tries to import tensorrt to see if it's supported.
        """
        if not hasattr(self, "_tensorrt_is_supported"):
            try:
                import tensorrt

                tensorrt.__version__  # quiet importchecker
                self._tensorrt_is_supported = True
            except Exception as ex:
                logger.info("TensorRT is disabled.")
                logger.debug("{0}: {1}".format(type(ex).__name__, ex))
                self._tensorrt_is_supported = False
        return self._tensorrt_is_supported

    @property
    def tensorrt_is_enabled(self) -> bool:
        """
        By default this is always enabled. This is independent from supported/ready.
        """
        if not hasattr(self, "_tensorrt_enabled"):
            self._tensorrt_enabled = True
        return self._tensorrt_enabled

    @tensorrt_is_enabled.setter
    def tensorrt_is_enabled(self, new_enabled: bool) -> None:
        """
        Disables or enables TensorRT.
        """
        if new_enabled != self.tensorrt_is_enabled and self.tensorrt_is_ready:
            self.unload_pipeline("TensorRT enabled or disabled")
        self._tensorrt_enabled = new_enabled

    @property
    def tensorrt_is_ready(self) -> bool:
        """
        Checks to determine if Tensor RT is ready based on the existence of engines.
        """
        if not self.tensorrt_is_supported:
            return False
        from enfugue.diffusion.rt.engine import Engine

        trt_ready = True
        if "vae" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.model_tensorrt_vae_dir))
        if "clip" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.model_tensorrt_clip_dir))
        if self.controlnet is not None or self.TENSORRT_ALWAYS_USE_CONTROLLED_UNET:
            if "unet" in self.TENSORRT_STAGES:
                trt_ready = trt_ready and os.path.exists(
                    Engine.get_engine_path(self.model_tensorrt_controlled_unet_dir)
                )
        elif "unet" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.model_tensorrt_unet_dir))
        return trt_ready

    @property
    def refiner_tensorrt_is_ready(self) -> bool:
        """
        Checks to determine if Tensor RT is ready based on the existence of engines for the refiner
        """
        if not self.tensorrt_is_supported:
            return False
        if self.refiner is None:
            return False
        from enfugue.diffusion.rt.engine import Engine

        trt_ready = True
        if "vae" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.refiner_tensorrt_vae_dir))
        if "clip" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.refiner_tensorrt_clip_dir))
        if self.controlnet is not None or self.TENSORRT_ALWAYS_USE_CONTROLLED_UNET:
            if "unet" in self.TENSORRT_STAGES:
                trt_ready = trt_ready and os.path.exists(
                    Engine.get_engine_path(self.refiner_tensorrt_controlled_unet_dir)
                )
        elif "unet" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.refiner_tensorrt_unet_dir))
        return trt_ready

    @property
    def inpainter_tensorrt_is_ready(self) -> bool:
        """
        Checks to determine if Tensor RT is ready based on the existence of engines for the inpainter
        """
        if not self.tensorrt_is_supported:
            return False
        if self.inpainter is None:
            return False
        from enfugue.diffusion.rt.engine import Engine

        trt_ready = True
        if "vae" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.inpainter_tensorrt_vae_dir))
        if "clip" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.inpainter_tensorrt_clip_dir))
        if "unet" in self.TENSORRT_STAGES:
            trt_ready = trt_ready and os.path.exists(Engine.get_engine_path(self.inpainter_tensorrt_unet_dir))
        return trt_ready

    @property
    def build_tensorrt(self) -> bool:
        """
        Checks to see if TensorRT should be built based on configuration.
        """
        if not hasattr(self, "_build_tensorrt"):
            if not self.tensorrt_is_supported:
                self._build_tensorrt = False
            else:
                self._build_tensorrt = self.configuration.get("enfugue.tensorrt", False)
        return self._build_tensorrt

    @build_tensorrt.setter
    def build_tensorrt(self, new_build: bool) -> None:
        """
        Changes whether or not TensorRT engines should be built when absent
        """
        self._build_tensorrt = new_build
        if not self.tensorrt_is_ready and self.tensorrt_is_supported:
            self.unload_pipeline("preparing for TensorRT build")

    @property
    def use_tensorrt(self) -> bool:
        """
        Gets the ultimate decision on whether the tensorrt pipeline should be used.
        """
        return (self.tensorrt_is_ready or self.build_tensorrt) and self.tensorrt_is_enabled

    @property
    def refiner_use_tensorrt(self) -> bool:
        """
        Gets the ultimate decision on whether the tensorrt pipeline should be used for the refiner.
        """
        return (self.refiner_tensorrt_is_ready or self.build_tensorrt) and self.tensorrt_is_enabled

    @property
    def inpainter_use_tensorrt(self) -> bool:
        """
        Gets the ultimate decision on whether the tensorrt pipeline should be used for the inpainter.
        """
        return (self.inpainter_tensorrt_is_ready or self.build_tensorrt) and self.tensorrt_is_enabled

    @property
    def use_directml(self) -> bool:
        """
        Determine if directml should be used
        """
        import torch
        from enfugue.diffusion.util import directml_available

        return not torch.cuda.is_available() and directml_available()

    @property
    def pipeline_switch_mode(self) -> Optional[Literal["offload", "unload"]]:
        """
        Defines how to switch to pipelines.
        """
        if not hasattr(self, "_pipeline_switch_mode"):
            self._pipeline_switch_mode = self.configuration.get("enfugue.pipeline.switch", "offload")
        return self._pipeline_switch_mode

    @pipeline_switch_mode.setter
    def pipeline_switch_mode(self, mode: Optional[Literal["offload", "unload"]]) -> None:
        """
        Changes how pipelines get switched.
        """
        self._pipeline_switch_mode = mode

    @property
    def refiner_strength(self) -> float:
        """
        Gets the denoising strength of the refiner
        """
        if not hasattr(self, "_refiner_strength"):
            self._refiner_strength = self.configuration.get("enfugue.refiner.strength", 0.3)
        return self._refiner_strength

    @refiner_strength.setter
    def refiner_strength(self, new_strength: float) -> None:
        """
        Sets the denoising strength of the refiner
        """
        self._refiner_strength = new_strength

    @property
    def refiner_guidance_scale(self) -> float:
        """
        Gets the guidance_ cale of the refiner
        """
        if not hasattr(self, "_refiner_guidance_scale"):
            self._refiner_guidance_scale = self.configuration.get("enfugue.refiner.guidance_scale", 5.0)
        return self._refiner_guidance_scale

    @refiner_guidance_scale.setter
    def refiner_guidance_scale(self, new_guidance_scale: float) -> None:
        """
        Sets the guidance scale of the refiner
        """
        self._refiner_guidance_scale = new_guidance_scale

    @property
    def refiner_aesthetic_score(self) -> float:
        """
        Gets the refiner_aesthetic score for the refiner
        """
        if not hasattr(self, "_refiner_aesthetic_score"):
            self._refiner_aesthetic_score = self.configuration.get("enfugue.refiner.refiner_aesthetic_score", 6.0)
        return self._refiner_aesthetic_score

    @refiner_aesthetic_score.setter
    def refiner_aesthetic_score(self, new_refiner_aesthetic_score: float) -> None:
        """
        Sets the refiner_aesthetic score for the refiner
        """
        self._refiner_aesthetic_score = new_refiner_aesthetic_score

    @property
    def refiner_negative_aesthetic_score(self) -> float:
        """
        Gets the negative refiner_aesthetic score for the refiner
        """
        if not hasattr(self, "_refiner_negative_aesthetic_score"):
            self._refiner_negative_aesthetic_score = self.configuration.get(
                "enfugue.refiner.refiner_negative_aesthetic_score", 2.5
            )
        return self._refiner_negative_aesthetic_score

    @refiner_negative_aesthetic_score.setter
    def refiner_negative_aesthetic_score(self, new_refiner_negative_aesthetic_score: float) -> None:
        """
        Sets the negative refiner_aesthetic score for the refiner
        """
        self._refiner_negative_aesthetic_score = new_refiner_negative_aesthetic_score

    @property
    def pipeline_class(self) -> Type:
        """
        Gets the pipeline class to use.
        """
        if self.use_tensorrt:
            from enfugue.diffusion.rt.pipeline import EnfugueTensorRTStableDiffusionPipeline

            return EnfugueTensorRTStableDiffusionPipeline
        else:
            from enfugue.diffusion.pipeline import EnfugueStableDiffusionPipeline

            return EnfugueStableDiffusionPipeline

    @property
    def refiner_pipeline_class(self) -> Type:
        """
        Gets the pipeline class to use.
        """
        if self.refiner_use_tensorrt:
            from enfugue.diffusion.rt.pipeline import EnfugueTensorRTStableDiffusionPipeline

            return EnfugueTensorRTStableDiffusionPipeline
        else:
            from enfugue.diffusion.pipeline import EnfugueStableDiffusionPipeline

            return EnfugueStableDiffusionPipeline

    @property
    def inpainter_pipeline_class(self) -> Type:
        """
        Gets the pipeline class to use.
        """
        if self.inpainter_use_tensorrt:
            from enfugue.diffusion.rt.pipeline import EnfugueTensorRTStableDiffusionPipeline

            return EnfugueTensorRTStableDiffusionPipeline
        else:
            from enfugue.diffusion.pipeline import EnfugueStableDiffusionPipeline

            return EnfugueStableDiffusionPipeline

    @property
    def model(self) -> str:
        """
        Gets the configured model.
        """
        if not hasattr(self, "_model"):
            self._model = self.configuration.get("enfugue.model", DEFAULT_MODEL)
        return self._model

    @model.setter
    def model(self, new_model: Optional[str]) -> None:
        """
        Sets a new model. Destroys the pipeline.
        """
        if new_model is None:
            new_model = self.configuration.get("enfugue.model", DEFAULT_MODEL)
        if new_model.startswith("http"):
            new_model = self.check_download_checkpoint(new_model)
        elif not os.path.isabs(new_model):
            new_model = os.path.join(self.engine_checkpoints_dir, new_model)
        new_model_name, _ = os.path.splitext(os.path.basename(new_model))
        if self.model_name != new_model_name:
            self.unload_pipeline("model changing")
        self._model = new_model

    @property
    def model_name(self) -> str:
        """
        Gets just the basename of the model
        """
        return os.path.splitext(os.path.basename(self.model))[0]

    @property
    def has_refiner(self) -> bool:
        """
        Returns true if the refiner is set.
        """
        return self.refiner is not None

    @property
    def refiner(self) -> Optional[str]:
        """
        Gets the configured refiner.
        """
        if not hasattr(self, "_refiner"):
            self._refiner = self.configuration.get("enfugue.refiner", None)
        return self._refiner

    @refiner.setter
    def refiner(self, new_refiner: Optional[str]) -> None:
        """
        Sets a new refiner. Destroys the refiner pipelline.
        """
        if new_refiner is None:
            self._refiner = None
            return
        if new_refiner.startswith("http"):
            new_refiner = self.check_download_checkpoint(new_refiner)
        elif not os.path.isabs(new_refiner):
            new_refiner = os.path.join(self.engine_checkpoints_dir, new_refiner)
        new_refiner_name, _ = os.path.splitext(os.path.basename(new_refiner))
        if self.refiner_name != new_refiner_name:
            self.unload_refiner("model changing")
        self._refiner = new_refiner

    @property
    def refiner_name(self) -> Optional[str]:
        """
        Gets just the basename of the refiner
        """
        if self.refiner is None:
            return None
        return os.path.splitext(os.path.basename(self.refiner))[0]

    @property
    def has_inpainter(self) -> bool:
        """
        Returns true if the inpainter is set.
        """
        return self.inpainter is not None

    @property
    def inpainter(self) -> Optional[str]:
        """
        Gets the configured inpainter.
        """
        if not hasattr(self, "_inpainter"):
            self._inpainter = self.configuration.get("enfugue.inpainter", None)
        return self._inpainter

    @inpainter.setter
    def inpainter(self, new_inpainter: Optional[str]) -> None:
        """
        Sets a new inpainter. Destroys the inpainter pipelline.
        """
        if new_inpainter is None:
            self._inpainter = None
            return
        if new_inpainter.startswith("http"):
            new_inpainter = self.check_download_checkpoint(new_inpainter)
        elif not os.path.isabs(new_inpainter):
            new_inpainter = os.path.join(self.engine_checkpoints_dir, new_inpainter)
        new_inpainter_name, _ = os.path.splitext(os.path.basename(new_inpainter))
        if self.inpainter_name != new_inpainter_name:
            self.unload_inpainter("model changing")
        self._inpainter = new_inpainter

    @property
    def inpainter_name(self) -> Optional[str]:
        """
        Gets just the basename of the inpainter
        """
        if self.inpainter is None:
            return None
        return os.path.splitext(os.path.basename(self.inpainter))[0]

    @property
    def dtype(self) -> torch.dtype:
        """
        Gets the default or configured torch data type
        """
        if not hasattr(self, "_torch_dtype"):
            import torch

            if self.device.type == "cpu":
                logger.debug("Inferencing on CPU, defaulting to dtype bfloat16")
                self._torch_dtype = torch.bfloat16
            elif self.device.type == "mps":
                logger.debug("Inferencing on MPS, defaulting to dtype float32")
                self._torch_dtype = torch.float32
            elif self.device.type == "cuda":
                if torch.version.hip:
                    # ROCm
                    logger.debug("Inferencing on ROCm, defaulting to dtype float32")  # type: ignore[unreachable]
                    self._torch_dtype = torch.float
                else:
                    # Regular CUDA
                    logger.debug("Inferencing on CUDA, defaulting to dtype float16")
                    self._torch_dtype = torch.half
            else:
                configuration_dtype = self.configuration.get("enfugue.dtype", "float16")
                if configuration_dtype == "float16" or configuration_dtype == "half":
                    self._torch_dtype = torch.half
                elif configuration_dtype == "float32" or configuration_dtype == "float":
                    self._torch_dtype = torch.float
                else:
                    raise ConfigurationError("dtype incorrectly configured, use 'float16/half' or 'float32/float'")
        return self._torch_dtype

    @dtype.setter
    def dtype(self, new_dtype: Union[str, torch.dtype]) -> None:
        """
        Sets the torch dtype.
        Deletes the pipeline if it's different from the previous one.
        """
        if self.device.type == "cpu":
            raise ValueError("CPU-based diffusion can only use bfloat")
        if new_dtype == "float16" or new_dtype == "half":
            new_dtype = torch.half
        elif new_dtype == "float32" or new_dtype == "float":
            new_dtype = torch.float
        else:
            raise ConfigurationError("dtype incorrectly configured, use 'float16/half' or 'float32/float'")

        if getattr(self, "_torch_dtype", new_dtype) != new_dtype:
            self.unload_pipeline("data type changing")
            self.unload_refiner("data type changing")
            self.unload_inpainter("data type changing")

        self._torch_dtype = new_dtype

    @property
    def lora(self) -> List[Tuple[str, float]]:
        """
        Get LoRA added to the text encoder and UNet.
        """
        return getattr(self, "_lora", [])

    @lora.setter
    def lora(self, new_lora: Optional[Union[str, List[str], Tuple[str, float], List[Tuple[str, float]]]]) -> None:
        """
        Sets new LoRA. Destroys the pipeline.
        """
        if new_lora is None:
            if hasattr(self, "_lora") and len(self._lora) > 0:
                self.unload_pipeline("LoRA changing")
            self._lora: List[Tuple[str, float]] = []
            return

        lora: List[Tuple[str, float]] = []
        if isinstance(new_lora, list):
            for this_lora in new_lora:
                if isinstance(this_lora, list):
                    lora.append(tuple(this_lora))  # type: ignore[unreachable]
                elif isinstance(this_lora, tuple):
                    lora.append(this_lora)
                else:
                    lora.append((this_lora, 1))
        elif isinstance(new_lora, tuple):
            lora = [new_lora]
        else:
            lora = [(new_lora, 1)]

        for i, (model, weight) in enumerate(lora):
            if not os.path.isabs(model):
                model = os.path.join(self.engine_lora_dir, model)
            lora[i] = (model, weight)

        if getattr(self, "_lora", []) != lora:
            self.unload_pipeline("LoRA changing")
            self._lora = lora

    @property
    def lora_names_weights(self) -> List[Tuple[str, float]]:
        """
        Gets the basenames of any LoRA present.
        """
        return [(os.path.splitext(os.path.basename(lora))[0], weight) for lora, weight in self.lora]

    @property
    def lycoris(self) -> List[Tuple[str, float]]:
        """
        Get lycoris added to the text encoder and UNet.
        """
        return getattr(self, "_lycoris", [])

    @lycoris.setter
    def lycoris(self, new_lycoris: Optional[Union[str, List[str], Tuple[str, float], List[Tuple[str, float]]]]) -> None:
        """
        Sets new lycoris. Destroys the pipeline.
        """
        if new_lycoris is None:
            if hasattr(self, "_lycoris") and len(self._lycoris) > 0:
                self.unload_pipeline("LyCORIS changing")
            self._lycoris: List[Tuple[str, float]] = []
            return

        lycoris: List[Tuple[str, float]] = []
        if isinstance(new_lycoris, list):
            for this_lycoris in new_lycoris:
                if isinstance(this_lycoris, list):
                    lycoris.append(tuple(this_lycoris))  # type: ignore[unreachable]
                elif isinstance(this_lycoris, tuple):
                    lycoris.append(this_lycoris)
                else:
                    lycoris.append((this_lycoris, 1))
        elif isinstance(new_lycoris, tuple):
            lycoris = [new_lycoris]
        else:
            lycoris = [(new_lycoris, 1)]

        for i, (model, weight) in enumerate(lycoris):
            if not os.path.isabs(model):
                model = os.path.join(self.engine_lycoris_dir, model)
            lycoris[i] = (model, weight)

        if getattr(self, "_lycoris", []) != lycoris:
            self.unload_pipeline("LyCORIS changing")
            self._lycoris = lycoris

    @property
    def lycoris_names_weights(self) -> List[Tuple[str, float]]:
        """
        Gets the basenames of any lycoris present.
        """
        return [(os.path.splitext(os.path.basename(lycoris))[0], weight) for lycoris, weight in self.lycoris]

    @property
    def inversion(self) -> List[str]:
        """
        Get textual inversion added to the text encoder.
        """
        return getattr(self, "_inversion", [])

    @inversion.setter
    def inversion(self, new_inversion: Optional[Union[str, List[str]]]) -> None:
        """
        Sets new textual inversion. Destroys the pipeline.
        """
        if new_inversion is None:
            if hasattr(self, "_inversion") and len(self._inversion) > 0:
                self.unload_pipeline("Textual Inversions changing")
            self._inversion: List[str] = []
            return

        if not isinstance(new_inversion, list):
            new_inversion = [new_inversion]
        if getattr(self, "_inversion", []) != new_inversion:
            self.unload_pipeline("Textual Inversions changing")
            self._inversion = new_inversion

    @property
    def inversion_names(self) -> List[str]:
        """
        Gets the basenames of any textual inversions present.
        """
        return [os.path.splitext(os.path.basename(inversion))[0] for inversion in self.inversion]

    @property
    def model_diffusers_cache_dir(self) -> Optional[str]:
        """
        Ggets where the diffusers cache directory is saved for this model, if there is any.
        """
        if os.path.exists(os.path.join(self.model_diffusers_dir, "model_index.json")):
            return self.model_diffusers_dir
        elif os.path.exists(os.path.join(self.model_tensorrt_dir, "model_index.json")):
            return self.model_tensorrt_dir
        return None

    @property
    def engine_cache_exists(self) -> bool:
        """
        Gets whether or not the diffusers cache exists.
        """
        return self.model_diffusers_cache_dir is not None

    @property
    def refiner_diffusers_cache_dir(self) -> Optional[str]:
        """
        Ggets where the diffusers cache directory is saved for this refiner, if there is any.
        """
        if os.path.exists(os.path.join(self.refiner_diffusers_dir, "model_index.json")):
            return self.refiner_diffusers_dir
        elif os.path.exists(os.path.join(self.refiner_tensorrt_dir, "model_index.json")):
            return self.refiner_tensorrt_dir
        return None

    @property
    def refiner_engine_cache_exists(self) -> bool:
        """
        Gets whether or not the diffusers cache exists.
        """
        return self.refiner_diffusers_cache_dir is not None

    @property
    def inpainter_diffusers_cache_dir(self) -> Optional[str]:
        """
        Ggets where the diffusers cache directory is saved for this inpainter, if there is any.
        """
        if os.path.exists(os.path.join(self.inpainter_diffusers_dir, "model_index.json")):
            return self.inpainter_diffusers_dir
        elif os.path.exists(os.path.join(self.inpainter_tensorrt_dir, "model_index.json")):
            return self.inpainter_tensorrt_dir
        return None

    @property
    def inpainter_engine_cache_exists(self) -> bool:
        """
        Gets whether or not the diffusers cache exists.
        """
        return self.inpainter_diffusers_cache_dir is not None

    @property
    def always_cache(self) -> bool:
        """
        Returns true if the model should always be cached.
        """
        configured = self.configuration.get("enfugue.engine.always_cache", None)
        if configured:
            return configured
        return self.is_sdxl
    
    @property
    def always_cache_inpainter(self) -> bool:
        """
        Returns true if the inpainter model should always be cached.
        """
        configured = self.configuration.get("enfugue.engine.always_cache", None)
        if configured:
            return configured
        return self.inpainter_is_sdxl
    
    @property
    def always_cache_refiner(self) -> bool:
        """
        Returns true if the refiner model should always be cached.
        """
        configured = self.configuration.get("enfugue.engine.always_cache", None)
        if configured:
            return configured
        return self.refiner_is_sdxl

    @property
    def is_sdxl(self) -> bool:
        """
        If the model is cached, we can know for sure by checking for sdxl-exclusive models.
        Otherwise, we guess by file name.
        """
        if self.model_diffusers_cache_dir is not None:
            return os.path.exists(os.path.join(self.model_diffusers_cache_dir, "text_encoder_2"))  # type: ignore[arg-type]
        return "xl" in self.model_name.lower()

    @property
    def refiner_is_sdxl(self) -> bool:
        """
        If the refiner model is cached, we can know for sure by checking for sdxl-exclusive models.
        Otherwise, we guess by file name.
        """
        if not self.refiner_name:
            return False
        if self.refiner_diffusers_cache_dir is not None:
            return os.path.exists(os.path.join(self.refiner_diffusers_cache_dir, "text_encoder_2"))  # type: ignore[arg-type]
        return "xl" in self.refiner_name.lower()

    @property
    def inpainter_is_sdxl(self) -> bool:
        """
        If the inpainter model is cached, we can know for sure by checking for sdxl-exclusive models.
        Otherwise, we guess by file name.
        """
        if not self.inpainter_name:
            return False
        if self.inpainter_diffusers_cache_dir is not None:
            return os.path.exists(os.path.join(self.inpainter_diffusers_cache_dir, "text_encoder_2"))  # type: ignore[arg-type]
        return "xl" in self.inpainter_name.lower()

    def check_create_engine_cache(self) -> None:
        """
        Converts a .ckpt file to the directory structure from diffusers
        """
        if not self.engine_cache_exists:
            from diffusers.pipelines.stable_diffusion.convert_from_ckpt import (
                download_from_original_stable_diffusion_ckpt,
            )

            _, ext = os.path.splitext(self.model)
            pipe = download_from_original_stable_diffusion_ckpt(
                checkpoint_path=self.model,
                from_safetensors=ext == ".safetensors",
                num_in_channels=4,
            ).to(torch_dtype=self.dtype)
            pipe.save_pretrained(self.model_diffusers_dir)
            del pipe
            self.clear_memory()

    def check_create_refiner_engine_cache(self) -> None:
        """
        Converts a .safetensor file to diffusers cache
        """
        if not self.refiner_engine_cache_exists and self.refiner:
            from diffusers.pipelines.stable_diffusion.convert_from_ckpt import (
                download_from_original_stable_diffusion_ckpt,
            )

            _, ext = os.path.splitext(self.refiner)
            pipe = download_from_original_stable_diffusion_ckpt(
                checkpoint_path=self.refiner, from_safetensors=ext == ".safetensors", num_in_channels=4
            ).to(torch_dtype=self.dtype)
            pipe.save_pretrained(self.refiner_diffusers_dir)
            del pipe
            self.clear_memory()

    def check_create_inpainter_engine_cache(self) -> None:
        """
        Converts a .safetensor file to diffusers cache
        """
        if not self.inpainter_engine_cache_exists and self.inpainter:
            from diffusers.pipelines.stable_diffusion.convert_from_ckpt import (
                download_from_original_stable_diffusion_ckpt,
            )

            _, ext = os.path.splitext(self.inpainter)
            pipe = download_from_original_stable_diffusion_ckpt(
                checkpoint_path=self.inpainter, from_safetensors=ext == ".safetensors", num_in_channels=9
            ).to(torch_dtype=self.dtype)
            pipe.save_pretrained(self.inpainter_diffusers_dir)
            del pipe
            self.clear_memory()

    @property
    def pipeline(self) -> EnfugueStableDiffusionPipeline:
        """
        Instantiates the pipeline.
        """
        if not hasattr(self, "_pipeline"):
            if self.model.startswith("http"):
                # Base model, make sure it's downloaded here
                self.model = self.check_download_checkpoint(self.model)

            kwargs = {
                "cache_dir": self.engine_cache_dir,
                "engine_size": self.size,
                "chunking_size": self.chunking_size,
                "requires_safety_checker": self.safe,
                "torch_dtype": self.dtype,
                "cache_dir": self.engine_cache_dir,
            }

            vae = self.vae  # Load into memory here
            controlnet = self.controlnet  # Load into memory here

            if self.use_tensorrt:
                if "unet" in self.TENSORRT_STAGES:
                    if self.controlnet is None and not self.TENSORRT_ALWAYS_USE_CONTROLLED_UNET:
                        kwargs["unet_engine_dir"] = self.model_tensorrt_unet_dir
                    else:
                        kwargs["controlled_unet_engine_dir"] = self.model_tensorrt_controlled_unet_dir
                if "vae" in self.TENSORRT_STAGES:
                    kwargs["vae_engine_dir"] = self.model_tensorrt_vae_dir
                if "clip" in self.TENSORRT_STAGES:
                    kwargs["clip_engine_dir"] = self.model_tensorrt_clip_dir
                if not self.safe:
                    kwargs["safety_checker"] = None
                self.check_create_engine_cache()
                if self.model_diffusers_cache_dir is None:
                    raise IOError("Couldn't create engine cache, check logs.")
                if not self.is_sdxl:
                    kwargs["tokenizer_2"] = None
                    kwargs["text_encoder_2"] = None
                logger.debug(
                    f"Initializing pipeline from diffusers cache directory at {self.model_diffusers_cache_dir}. Arguments are {kwargs}"
                )
                if vae is not None:
                    kwargs["vae"] = vae
                pipeline = self.pipeline_class.from_pretrained(
                    self.model_diffusers_cache_dir, controlnet=controlnet, **kwargs
                )
            elif self.model_diffusers_cache_dir is not None:
                if not self.safe:
                    kwargs["safety_checker"] = None
                if not self.is_sdxl:
                    kwargs["tokenizer_2"] = None
                    kwargs["text_encoder_2"] = None
                logger.debug(
                    f"Initializing pipeline from diffusers cache directory at {self.model_diffusers_cache_dir}. Arguments are {kwargs}"
                )
                if vae is not None:
                    kwargs["vae"] = vae
                pipeline = self.pipeline_class.from_pretrained(
                    self.model_diffusers_cache_dir, controlnet=controlnet, **kwargs
                )
            else:
                kwargs["load_safety_checker"] = self.safe
                if self.vae_name is not None:
                    if self.vae_name == "mse":
                        kwargs["vae_path"] = VAE_MSE
                    elif self.vae_name == "ema":
                        kwargs["vae_path"] = VAE_EMA
                logger.debug(f"Initializing pipeline from checkpoint at {self.model}. Arguments are {kwargs}")
                pipeline = self.pipeline_class.from_ckpt(self.model, num_in_channels=4, controlnet=controlnet, **kwargs)
                if self.always_cache:
                    logger.debug("Saving pipeline to pretrained.")
                    pipeline.save_pretrained(self.model_diffusers_dir)
            if not self.tensorrt_is_ready:
                for lora, weight in self.lora:
                    logger.debug(f"Adding LoRA {lora} to pipeline")
                    pipeline.load_lora_weights(lora, multiplier=weight)
                for lycoris, weight in self.lycoris:
                    logger.debug(f"Adding lycoris {lycoris} to pipeline")
                    pipeline.load_lycoris_weights(lycoris, multiplier=weight)
                for inversion in self.inversion:
                    logger.debug(f"Adding textual inversion {inversion} to pipeline")
                    pipeline.load_textual_inversion(inversion)

            # load scheduler
            if self.scheduler is not None:
                pipeline.scheduler = self.scheduler.from_config(pipeline.scheduler_config)
            if self.multi_scheduler is not None:
                pipeline.multi_scheduler = self.multi_scheduler.from_config(pipeline.scheduler_config)
            self._pipeline = pipeline.to(self.device)
        return self._pipeline

    @pipeline.deleter
    def pipeline(self) -> None:
        """
        Eliminates any instantiated pipeline.
        """
        if hasattr(self, "_pipeline"):
            logger.debug("Deleting pipeline.")
            del self._pipeline
            if hasattr(self, "_pipeline_is_offloaded"):
                del self._pipeline_is_offloaded
            self.clear_memory()
        else:
            logger.debug("Pipeline delete called, but no pipeline present. This is not an error.")

    @property
    def refiner_pipeline(self) -> EnfugueStableDiffusionPipeline:
        """
        Instantiates the refiner pipeline.
        """
        if not self.refiner:
            raise ValueError("No refiner set")
        if not hasattr(self, "_refiner_pipeline"):
            if self.refiner.startswith("http"):
                # Base refiner, make sure it's downloaded here
                self.refiner = self.check_download_checkpoint(self.refiner)

            kwargs = {
                "cache_dir": self.engine_cache_dir,
                "engine_size": self.refiner_size,
                "chunking_size": self.chunking_size,
                "torch_dtype": self.dtype,
                "requires_safety_checker": False,
                "controlnet": None,
            }

            if self.vae_name == "xl" and not self.refiner_is_sdxl:
                vae = None
            elif self.vae_name != "xl" and self.refiner_is_sdxl:
                vae = None
            else:
                vae = self.vae  # Load into memory here

            if self.refiner_use_tensorrt:
                if "unet" in self.TENSORRT_STAGES:
                    if self.controlnet is None and not self.TENSORRT_ALWAYS_USE_CONTROLLED_UNET:
                        kwargs["unet_engine_dir"] = self.refiner_tensorrt_unet_dir
                    else:
                        kwargs["controlled_unet_engine_dir"] = self.refiner_tensorrt_controlled_unet_dir

                """
                if "controlnet" in self.TENSORRT_STAGES and self.controlnet is not None:
                    kwargs["controlnet_engine_dir"] = self.refiner_tensorrt_controlnet_dir
                """

                if "vae" in self.TENSORRT_STAGES:
                    kwargs["vae_engine_dir"] = self.refiner_tensorrt_vae_dir
                if "clip" in self.TENSORRT_STAGES:
                    kwargs["clip_engine_dir"] = self.refiner_tensorrt_clip_dir

                self.check_create_refiner_engine_cache()
                if self.refiner_is_sdxl:
                    kwargs["text_encoder"] = None
                    kwargs["tokenizer"] = None
                    kwargs["requires_aesthetic_score"] = True
                else:
                    kwargs["text_encoder_2"] = None
                    kwargs["tokenizer_2"] = None

                logger.debug(
                    f"Initializing refiner pipeline from diffusers cache directory at {self.refiner_diffusers_cache_dir}. Arguments are {kwargs}"
                )

                if vae is not None:
                    kwargs["vae"] = vae

                refiner_pipeline = self.refiner_pipeline_class.from_pretrained(
                    self.refiner_diffusers_cache_dir,
                    safety_checker=None,
                    **kwargs,
                )
            elif self.refiner_engine_cache_exists:
                if self.refiner_is_sdxl:
                    kwargs["text_encoder"] = None
                    kwargs["tokenizer"] = None
                    kwargs["requires_aesthetic_score"] = True
                else:
                    kwargs["text_encoder_2"] = None
                    kwargs["tokenizer_2"] = None
                logger.debug(
                    f"Initializing refiner pipeline from diffusers cache directory at {self.refiner_diffusers_cache_dir}. Arguments are {kwargs}"
                )
                if vae is not None:
                    kwargs["vae"] = vae
                refiner_pipeline = self.refiner_pipeline_class.from_pretrained(
                    self.refiner_diffusers_cache_dir,
                    safety_checker=None,
                    # controlnet=controlnet,
                    **kwargs,
                )
            else:
                if self.vae_name is not None:
                    if self.vae_name == "mse":
                        kwargs["vae_path"] = VAE_MSE
                    elif self.vae_name == "ema":
                        kwargs["vae_path"] = VAE_EMA
                logger.debug(f"Initializing refiner pipeline from checkpoint at {self.refiner}. Arguments are {kwargs}")
                refiner_pipeline = self.refiner_pipeline_class.from_ckpt(
                    self.refiner,
                    num_in_channels=4,
                    load_safety_checker=False,
                    # controlnet=controlnet,
                    **kwargs,
                )
                if self.always_cache_refiner:
                    logger.debug("Saving pipeline to pretrained.")
                    refiner_pipeline.save_pretrained(self.refiner_diffusers_dir)
            if not self.refiner_tensorrt_is_ready:
                for lora, weight in self.lora:
                    logger.debug(f"Adding LoRA {lora} to refiner pipeline")
                    refiner_pipeline.load_lora_weights(lora, multiplier=weight)
                for lycoris, weight in self.lycoris:
                    logger.debug(f"Adding lycoris {lycoris} to refiner pipeline")
                    refiner_pipeline.load_lycoris_weights(lycoris, multiplier=weight)
                for inversion in self.inversion:
                    logger.debug(f"Adding textual inversion {inversion} to refiner pipeline")
                    refiner_pipeline.load_textual_inversion(inversion)
            
            # load scheduler
            if self.scheduler is not None:
                refiner_pipeline.scheduler = self.scheduler.from_config(refiner_pipeline.scheduler_config)
            if self.multi_scheduler is not None:
                refiner_pipeline.multi_scheduler = self.multi_scheduler.from_config(refiner_pipeline.scheduler_config)
            self._refiner_pipeline = refiner_pipeline.to(self.device)
        return self._refiner_pipeline

    @refiner_pipeline.deleter
    def refiner_pipeline(self) -> None:
        """
        Unloads the refiner pipeline if present.
        """
        if hasattr(self, "_refiner_pipeline"):
            logger.debug("Deleting refiner pipeline.")
            del self._refiner_pipeline
            if hasattr(self, "_refiner_pipeline_is_offloaded"):
                del self._refiner_pipeline_is_offloaded
            self.clear_memory()
        else:
            logger.debug("Refiner pipeline delete called, but no refiner pipeline present. This is not an error.")

    @property
    def inpainter_pipeline(self) -> EnfugueStableDiffusionPipeline:
        """
        Instantiates the inpainter pipeline.
        """
        if not hasattr(self, "_inpainter_pipeline"):
            if not self.inpainter:
                if self.is_sdxl:
                    logger.warning(f"Main model is SDXL, and no configured inpainting checkpoint. There is no inpainting checkpoint for SDXL; switching to default SD 1.5 inpainting checkpoint. You may wish to configure a more fine-tuned SD 1.5 inpainting checkpoint for better results.")
                    self.inpainter = DEFAULT_INPAINTING_MODEL
                else:
                    current_checkpoint_path = self.model
                    default_checkpoint_name, _ = os.path.splitext(os.path.basename(DEFAULT_MODEL))
                    default_inpainting_name, _ = os.path.splitext(os.path.basename(DEFAULT_INPAINTING_MODEL))
                    checkpoint_name, ext = os.path.splitext(os.path.basename(current_checkpoint_path))

                    if default_checkpoint_name == checkpoint_name:
                        self.inpainter = DEFAULT_INPAINTING_MODEL
                    else:
                        target_checkpoint_name = f"{checkpoint_name}-inpainting"
                        target_checkpoint_path = os.path.join(
                            os.path.dirname(current_checkpoint_path), f"{target_checkpoint_name}{ext}"
                        )
                        if not os.path.exists(target_checkpoint_path):
                            logger.info(f"Creating inpainting checkpoint from {current_checkpoint_path}")
                            self.create_inpainting_checkpoint(current_checkpoint_path, target_checkpoint_path)
                        self.inpainter = target_checkpoint_path
            if self.inpainter.startswith("http"):
                self.inpainter = self.check_download_checkpoint(self.inpainter)

            kwargs = {
                "cache_dir": self.engine_cache_dir,
                "engine_size": self.inpainter_size,
                "chunking_size": self.chunking_size,
                "torch_dtype": self.dtype,
                "requires_safety_checker": self.safe,
                "requires_aesthetic_score": False,
                "controlnet": None,
            }

            if self.vae_name == "xl" and not self.inpainter_is_sdxl:
                vae = None
            else:
                vae = self.vae  # Load into memory here

            if self.inpainter_use_tensorrt:
                if "unet" in self.TENSORRT_STAGES:
                    kwargs["unet_engine_dir"] = self.inpainter_tensorrt_unet_dir
                if "vae" in self.TENSORRT_STAGES:
                    kwargs["vae_engine_dir"] = self.inpainter_tensorrt_vae_dir
                if "clip" in self.TENSORRT_STAGES:
                    kwargs["clip_engine_dir"] = self.inpainter_tensorrt_clip_dir

                self.check_create_inpainter_engine_cache()
                
                if not self.safe:
                    kwargs["safety_checker"] = None
                if not self.inpainter_is_sdxl:
                    kwargs["text_encoder_2"] = None
                    kwargs["tokenizer_2"] = None
                
                logger.debug(
                    f"Initializing inpainter pipeline from diffusers cache directory at {self.inpainter_diffusers_cache_dir}. Arguments are {kwargs}"
                )

                if vae is not None:
                    kwargs["vae"] = vae

                inpainter_pipeline = self.inpainter_pipeline_class.from_pretrained(
                    self.inpainter_diffusers_cache_dir, **kwargs
                )
            elif self.inpainter_engine_cache_exists:
                logger.debug(
                    f"Initializing inpainter pipeline from diffusers cache directory at {self.inpainter_diffusers_cache_dir}. Arguments are {kwargs}"
                )
                if not self.safe:
                    kwargs["safety_checker"] = None
                if not self.inpainter_is_sdxl:
                    kwargs["text_encoder_2"] = None
                    kwargs["tokenizer_2"] = None
                    kwargs["text_encoder_2"] = None
                    kwargs["tokenizer_2"] = None
                if vae is not None:
                    kwargs["vae"] = vae

                inpainter_pipeline = self.inpainter_pipeline_class.from_pretrained(
                    self.inpainter_diffusers_cache_dir, **kwargs
                )
            else:
                logger.debug(
                    f"Initializing inpainter pipeline from checkpoint at {self.inpainter}. Arguments are {kwargs}"
                )
                if self.vae_name is not None:
                    if self.vae_name == "mse":
                        kwargs["vae_path"] = VAE_MSE
                    elif self.vae_name == "ema":
                        kwargs["vae_path"] = VAE_EMA
                inpainter_pipeline = self.inpainter_pipeline_class.from_ckpt(
                    self.inpainter, num_in_channels=9, load_safety_checker=self.safe, **kwargs
                )
                if self.always_cache_inpainter:
                    logger.debug("Saving inpainter pipeline to pretrained cache.")
                    inpainter_pipeline.save_pretrained(self.inpainter_diffusers_dir)
            if not self.inpainter_tensorrt_is_ready:
                for lora, weight in self.lora:
                    logger.debug(f"Adding LoRA {lora} to inpainter pipeline")
                    inpainter_pipeline.load_lora_weights(lora, multiplier=weight)
                for lycoris, weight in self.lycoris:
                    logger.debug(f"Adding lycoris {lycoris} to inpainter pipeline")
                    inpainter_pipeline.load_lycoris_weights(lycoris, multiplier=weight)
                for inversion in self.inversion:
                    logger.debug(f"Adding textual inversion {inversion} to inpainter pipeline")
                    inpainter_pipeline.load_textual_inversion(inversion)
            # load scheduler
            if self.scheduler is not None:
                inpainter_pipeline.scheduler = self.scheduler.from_config(inpainter_pipeline.scheduler_config)
            if self.multi_scheduler is not None:
                inpainter_pipeline.multi_scheduler = self.multi_scheduler.from_config(
                    inpainter_pipeline.scheduler_config
                )
            self._inpainter_pipeline = inpainter_pipeline.to(self.device)
        return self._inpainter_pipeline

    @inpainter_pipeline.deleter
    def inpainter_pipeline(self) -> None:
        """
        Unloads the inpainter pipeline if present.
        """
        if hasattr(self, "_inpainter_pipeline"):
            logger.debug("Deleting inpainter pipeline.")
            del self._inpainter_pipeline
            if hasattr(self, "_inpainter_pipeline_is_offloaded"):
                del self._inpainter_pipeline_is_offloaded
            self.clear_memory()

    @property
    def pipeline_is_offloaded(self) -> bool:
        """
        Returns true if the pipeline has been sent to CPU.
        """
        return hasattr(self, "_pipeline") and getattr(self, "_pipeline_is_offloaded", False)

    def unload_pipeline(self, reason: str) -> None:
        """
        Calls the pipeline deleter.
        """
        if hasattr(self, "_pipeline"):
            logger.debug(f'Unloading pipeline for reason "{reason}"')
            del self.pipeline

    def offload_pipeline(self) -> None:
        """
        Offloads the pipeline to CPU if present.
        """
        if hasattr(self, "_pipeline") and not self.pipeline_is_offloaded:
            import torch

            logger.debug("Offloading pipeline to CPU.")
            self._pipeline = self._pipeline.to("cpu", torch_dtype=torch.float32)
            self._pipeline_is_offloaded = True
            self.clear_memory()

    def reload_pipeline(self) -> None:
        """
        Reloads the pipeline to the device if present.
        """
        if self.pipeline_is_offloaded:
            logger.debug("Reloading pipeline from CPU.")
            self._pipeline = self._pipeline.to(self.device, torch_dtype=self.dtype)
            self._pipeline_is_offloaded = False

    @property
    def refiner_is_offloaded(self) -> bool:
        """
        Returns true if the refiner has been sent to CPU.
        """
        return hasattr(self, "_refiner_pipeline") and getattr(self, "_refiner_pipeline_is_offloaded", False)

    def unload_refiner(self, reason: str) -> None:
        """
        Calls the refiner deleter.
        """
        if hasattr(self, "_refiner_pipeline"):
            logger.debug(f'Unloading refiner pipeline for reason "{reason}"')
            del self.refiner_pipeline

    def offload_refiner(self) -> None:
        """
        Offloads the pipeline to CPU if present.
        """
        if hasattr(self, "_refiner_pipeline") and not self.refiner_is_offloaded:
            import torch

            logger.debug("Offloading refiner to CPU")
            self._refiner_pipeline = self._refiner_pipeline.to("cpu", torch_dtype=torch.float32)
            self._refiner_pipeline_is_offloaded = True
            self.clear_memory()

    def reload_refiner(self) -> None:
        """
        Reloads the pipeline to the device if present.
        """
        if self.refiner_is_offloaded:
            logger.debug("Reloading refiner from CPU")
            self._refiner_pipeline = self._refiner_pipeline.to(self.device, torch_dtype=self.dtype)
            self._refiner_pipeline_is_offloaded = False

    @property
    def inpainter_is_offloaded(self) -> bool:
        """
        Returns true if the inpainter has been sent to CPU.
        """
        return hasattr(self, "_inpainter_pipeline") and getattr(self, "_inpainter_pipeline_is_offloaded", False)

    def unload_inpainter(self, reason: str) -> None:
        """
        Calls the inpainter deleter.
        """
        if hasattr(self, "_inpainter_pipeline"):
            logger.debug(f'Unloading inpainter pipeline for reason "{reason}"')
            del self.inpainter_pipeline

    def offload_inpainter(self) -> None:
        """
        Offloads the pipeline to CPU if present.
        """
        if hasattr(self, "_inpainter_pipeline") and not self.inpainter_is_offloaded:
            import torch

            logger.debug("Offloading inpainter to CPU")
            self._inpainter_pipeline = self._inpainter_pipeline.to("cpu", torch_dtype=torch.float32)
            self._inpainter_pipeline_is_offloaded = True
            self.clear_memory()

    def reload_inpainter(self) -> None:
        """
        Reloads the pipeline to the device if present.
        """
        if self.inpainter_is_offloaded:
            logger.debug("Reloading inpainter from CPU")
            self._inpainter_pipeline_is_offloaded = False
            self._inpainter_pipeline = self._inpainter_pipeline.to(self.device, torch_dtype=self.dtype)

    @property
    def upscaler(self) -> Upscaler:
        """
        Gets the GAN upscaler
        """
        if not hasattr(self, "_upscaler"):
            from enfugue.diffusion.upscale import Upscaler

            self._upscaler = Upscaler(model_dir=self.engine_other_dir, device=self.device, dtype=self.dtype)
        return self._upscaler

    @upscaler.deleter
    def upscaler(self) -> None:
        """
        Deletes the upscaler to save VRAM.
        """
        if hasattr(self, "_upscaler"):
            logger.debug("Deleting upscaler.")
            del self._upscaler
            self.clear_memory()

    @property
    def edge_detector(self) -> EdgeDetector:
        """
        Gets the edge detector.
        """
        if not hasattr(self, "_edge_detector"):
            from enfugue.diffusion.edge.detect import EdgeDetector

            self._edge_detector = EdgeDetector(self.engine_other_dir)
        return self._edge_detector

    @property
    def depth_detector(self) -> DepthDetector:
        """
        Gets the depth detector.
        """
        if not hasattr(self, "_depth_detector"):
            from enfugue.diffusion.depth.detect import DepthDetector

            self._depth_detector = DepthDetector(self.engine_other_dir, self.device)
        return self._depth_detector

    @property
    def pose_detector(self) -> PoseDetector:
        """
        Gets the pose detector.
        """
        if not hasattr(self, "_pose_detector"):
            from enfugue.diffusion.pose.detect import PoseDetector

            self._pose_detector = PoseDetector(self.engine_other_dir, self.device)
        return self._pose_detector

    def unload_upscaler(self) -> None:
        """
        Calls the upscaler deleter.
        """
        del self.upscaler

    def get_controlnet(self, controlnet: Optional[str] = None) -> Optional[ControlNetModel]:
        """
        Loads a controlnet
        """
        if controlnet is None:
            return None
        from diffusers.models import ControlNetModel

        expected_controlnet_location = os.path.join(self.engine_cache_dir, "models--" + controlnet.replace("/", "--"))

        if not os.path.exists(expected_controlnet_location):
            logger.info(
                f"Controlnet {controlnet} does not exist in cache directory {self.engine_cache_dir}, it will be downloaded."
            )

        result = ControlNetModel.from_pretrained(
            controlnet,
            torch_dtype=torch.half,
            cache_dir=self.engine_cache_dir,
        )
        return result

    @property
    def controlnet(self) -> Optional[ControlNetModel]:
        """
        Gets the configured controlnet (or none.)
        """
        if not hasattr(self, "_controlnet"):
            self._controlnet = self.get_controlnet(self.controlnet_name)
        return self._controlnet

    @controlnet.setter
    def controlnet(
        self,
        new_controlnet: Optional[
            Literal["canny", "tile", "mlsd", "hed", "scribble", "inpaint", "depth", "normal", "pose"]
        ],
    ) -> None:
        """
        Sets a new controlnet.
        """
        pretrained_path = None
        if new_controlnet == "canny":
            pretrained_path = CONTROLNET_CANNY
        elif new_controlnet == "mlsd":
            pretrained_path = CONTROLNET_MLSD
        elif new_controlnet == "hed":
            pretrained_path = CONTROLNET_HED
        elif new_controlnet == "tile":
            pretrained_path = CONTROLNET_TILE
        elif new_controlnet == "scribble":
            pretrained_path = CONTROLNET_SCRIBBLE
        elif new_controlnet == "inpaint":
            pretrained_path = CONTROLNET_INPAINT
        elif new_controlnet == "depth":
            pretrained_path = CONTROLNET_DEPTH
        elif new_controlnet == "normal":
            pretrained_path = CONTROLNET_NORMAL
        elif new_controlnet == "pose":
            pretrained_path = CONTROLNET_POSE
        if pretrained_path is None and new_controlnet is not None:
            logger.error(f"Unsupported controlnet {new_controlnet}")

        existing_controlnet = getattr(self, "_controlnet", None)

        if (
            (existing_controlnet is None and new_controlnet is not None)
            or (existing_controlnet is not None and new_controlnet is None)
            or (existing_controlnet is not None and self.controlnet_name != new_controlnet)
        ):
            self.unload_pipeline("ControlNet changing")
            if new_controlnet is not None:
                self._controlnet_name = new_controlnet
                self._controlnet = self.get_controlnet(pretrained_path)
            else:
                self._controlnet_name = None  # type: ignore
                self._controlnet = None

    @property
    def controlnet_name(self) -> Optional[str]:
        """
        Gets the name of the control net, if one was set.
        """
        if not hasattr(self, "_controlnet_name"):
            self._controlnet_name = self.configuration.get("enfugue.controlnet", None)
        return self._controlnet_name

    def check_download_checkpoint(self, remote_url: str) -> str:
        """
        Downloads a checkpoint directly to the checkpoints folder.
        """
        output_file = os.path.basename(remote_url)
        output_path = os.path.join(self.engine_checkpoints_dir, output_file)
        check_download(remote_url, output_path)
        return output_path

    def __call__(
        self,
        refiner_strength: Optional[float] = None,
        refiner_guidance_scale: Optional[float] = None,
        refiner_aesthetic_score: Optional[float] = None,
        refiner_negative_aesthetic_score: Optional[float] = None,
        scale_to_refiner_size: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Passes an invocation down to the pipeline, doing whatever it needs to do to initialize it.
        Will switch between inpainting and non-inpainting models
        """
        logger.debug(f"Calling pipeline with arguments {redact(kwargs)}")
        self.start_keepalive()
        try:
            inpainting = kwargs.get("mask", None) is not None

            if inpainting:
                size = self.inpainter_size
                pipeline = self.inpainter_pipeline
                if self.pipeline_switch_mode == "offload":
                    self.offload_pipeline()
                elif self.pipeline_switch_mode == "unload":
                    self.unload_pipeline("switching to inpainting")
                self.reload_inpainter()
            else:
                size = self.size
                pipeline = self.pipeline
                if self.pipeline_switch_mode == "offload":
                    self.offload_inpainter()
                elif self.pipeline_switch_mode == "unload":
                    self.unload_inpainter("switching from inpainting")
                self.reload_pipeline()

            called_width = kwargs.get("width", size)
            called_height = kwargs.get("height", size)
            chunk_size = kwargs.get("chunking_size", self.chunking_size)

            if called_width < size:
                self.tensorrt_is_enabled = False
                logger.info(f"Width ({called_width}) less than configured width ({size}), disabling TensorRT")
            elif called_height < size:
                logger.info(f"Height ({called_height}) less than configured height ({size}), disabling TensorRT")
                self.tensorrt_is_enabled = False
            elif (called_width != size or called_height != size) and not chunk_size:
                logger.info(f"Dimensions do not match size of engine and chunking is disabled, disabling TensorRT")
                self.tensorrt_is_enabled = False
            else:
                self.tenssort_is_enabled = True

            if inpainting:
                pipe = self.inpainter_pipeline
            else:
                pipe = self.pipeline

            self.stop_keepalive()
            result = pipe(generator=self.generator, **kwargs)

            if self.refiner is not None:
                if refiner_strength is not None and refiner_strength <= 0:
                    logger.debug("Refinement strength is zero, not refining.")
                else:
                    self.start_keepalive()
                    if kwargs.get("latent_callback", None) is not None:
                        kwargs["latent_callback"](result["images"])  # type: ignore
                    if self.pipeline_switch_mode == "offload":
                        if inpainting:
                            self.offload_inpainter()
                        else:
                            self.offload_pipeline()
                        self.reload_refiner()
                    elif self.pipeline_switch_mode == "unload":
                        if inpainting:
                            self.unload_inpainter("switching to refining")
                        else:
                            self.unload_pipeline("switching to refining")
                    else:
                        logger.debug("Keeping pipeline in memory")

                    for i, image in enumerate(result["images"]):  # type: ignore
                        is_nsfw = "nsfw_content_detected" in result and result["nsfw_content_detected"][i]  # type: ignore
                        if is_nsfw:
                            logger.info(f"Result {i} has NSFW content, not refining.")
                            continue
                        kwargs.pop("image", None)  # Remove any previous image
                        kwargs.pop("mask", None)  # Remove any previous mask

                        kwargs["strength"] = refiner_strength if refiner_strength else self.refiner_strength
                        kwargs["guidance_scale"] = (
                            refiner_guidance_scale if refiner_guidance_scale else self.refiner_guidance_scale
                        )
                        kwargs["aesthetic_score"] = (
                            refiner_aesthetic_score if refiner_aesthetic_score else self.refiner_aesthetic_score
                        )
                        kwargs["negative_aesthetic_score"] = (
                            refiner_negative_aesthetic_score
                            if refiner_negative_aesthetic_score
                            else self.refiner_negative_aesthetic_score
                        )

                        width, height = image.size
                        image_scale = 1
                        if (width < self.refiner_size or height < self.refiner_size) and scale_to_refiner_size:
                            if width < self.refiner_size:
                                image_scale = self.refiner_size / width
                            if height < self.refiner_size:
                                image_scale = max(image_scale, self.refiner_size / height)
                            new_width = 8 * round((width * image_scale) / 8)
                            new_height = 8 * round((height * image_scale) / 8)
                            image = image.resize((new_width, new_height))
                            kwargs["width"] = new_width
                            kwargs["height"] = new_height
                            if "latent_callback" in kwargs and kwargs.get("latent_callback_type", "pil") == "pil":
                                original_callback = kwargs["latent_callback"]
                                def resize_callback(images: List[PIL.Image.Image]) -> None:
                                    original_callback([
                                        image.resize((width, height))
                                        for image in images
                                    ])
                                kwargs["latent_callback"] = resize_callback
                            logger.debug(f"Scaling image up to {new_width}×{new_height} (×{image_scale:.3f}) for refiner")

                        logger.debug(f"Refining result {i} with arguments {kwargs}")
                        self.stop_keepalive()  # This checks, we can call it all we want
                        refined_image = self.refiner_pipeline(  # type: ignore
                            generator=self.generator, image=image, **kwargs
                        )["images"][0]  # type: ignore
                        if image_scale != 1:
                            logger.debug(f"Scaling refined image back down to {width}×{height}")
                            refined_image = refined_image.resize((width, height)) # type: ignore
                        result["images"][i] = refined_image # type: ignore
                    if self.pipeline_switch_mode == "offload":
                        self.offload_refiner()
                    elif self.pipeline_switch_mode == "unload":
                        self.unload_refiner("unloading for next inference")
                    else:
                        logger.debug("Keeping refiner in memory")
            return result
        finally:
            self.tensorrt_is_enabled = True
            self.stop_keepalive()

    def write_model_metadata(self, path: str) -> None:
        """
        Writes metadata for TensorRT to a json file
        """
        if "controlnet" in path:
            dump_json(path, {"size": self.size, "controlnet": self.controlnet_name})
        else:
            dump_json(
                path,
                {
                    "size": self.size,
                    "lora": self.lora_names_weights,
                    "lycoris": self.lycoris_names_weights,
                    "inversion": self.inversion_names,
                },
            )

    @staticmethod
    def get_status(
        engine_root: str,
        model: str,
        size: Optional[int] = None,
        lora: Optional[Union[str, Tuple[str, float], List[Union[str, Tuple[str, float]]]]] = None,
        lycoris: Optional[Union[str, Tuple[str, float], List[Union[str, Tuple[str, float]]]]] = None,
        inversion: Optional[Union[str, List[str]]] = None,
        controlnet: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, bool]:
        """
        Gets the TensorRT status for an individual model.
        """
        tensorrt_is_supported = False

        try:
            import tensorrt

            tensorrt.__version__  # quiet importchecker
            tensorrt_is_supported = True
        except Exception as ex:
            logger.info("TensorRT is disabled.")
            logger.debug("{0}: {1}".format(type(ex).__name__, ex))
            pass

        if model.endswith(".ckpt") or model.endswith(".safetensors"):
            model, _ = os.path.splitext(os.path.basename(model))
        else:
            model = os.path.basename(model)

        model_dir = os.path.join(engine_root, "tensorrt", model)
        model_cache_dir = os.path.join(engine_root, "diffusers", model)
        model_index: Optional[str] = os.path.join(model_dir, "model_index.json")
        if not os.path.exists(model_index):  # type: ignore
            model_index = os.path.join(model_cache_dir, "model_index.json")
            if not os.path.exists(model_index):
                model_index = None

        if model_index is not None:
            # Cached model, get details
            model_is_sdxl = os.path.exists(os.path.join(os.path.dirname(model_index), "tokenizer_2"))
        else:
            model_is_sdxl = "xl" in model.lower()

        if not tensorrt_is_supported or model_is_sdxl:
            return {"supported": False, "xl": model_is_sdxl, "ready": False}

        if size is None:
            size = DiffusionPipelineManager.DEFAULT_SIZE

        if inversion is None:
            inversion = []
        elif not isinstance(inversion, list):
            inversion = [inversion]

        inversion_key = []
        for inversion_part in inversion:
            inversion_name, ext = os.path.splitext(os.path.basename(inversion_part))
            inversion_key.append(inversion_name)

        if lora is None:
            lora = []
        elif not isinstance(lora, list):
            lora = [lora]

        lora_key = []
        for lora_part in lora:
            if isinstance(lora_part, tuple):
                lora_path, lora_weight = lora_part
            else:
                lora_path, lora_weight = lora_part, 1.0
            lora_name, ext = os.path.splitext(os.path.basename(lora_path))
            lora_key.append((lora_name, lora_weight))

        if lycoris is None:
            lycoris = []
        elif not isinstance(lycoris, list):
            lycoris = [lycoris]

        lycoris_key = []
        for lycoris_part in lycoris:
            if isinstance(lycoris_part, tuple):
                lycoris_path, lycoris_weight = lycoris_part
            else:
                lycoris_path, lycoris_weight = lycoris_part, 1.0
            lycoris_name, ext = os.path.splitext(os.path.basename(lycoris_path))
            lycoris_key.append((lycoris_name, lycoris_weight))

        clip_ready = "clip" not in DiffusionPipelineManager.TENSORRT_STAGES
        vae_ready = "vae" not in DiffusionPipelineManager.TENSORRT_STAGES
        unet_ready = "unet" not in DiffusionPipelineManager.TENSORRT_STAGES
        controlled_unet_ready = unet_ready

        if not clip_ready:
            clip_key = DiffusionPipelineManager.get_clip_key(
                size, lora=lora_key, lycoris=lycoris_key, inversion=inversion_key
            )
            clip_plan = os.path.join(model_dir, "clip", clip_key, "engine.plan")
            clip_ready = os.path.exists(clip_plan)

        if not vae_ready:
            vae_key = DiffusionPipelineManager.get_vae_key(
                size, lora=lora_key, lycoris=lycoris_key, inversion=inversion_key
            )
            vae_plan = os.path.join(model_dir, "vae", vae_key, "engine.plan")
            vae_ready = os.path.exists(vae_plan)

        if not unet_ready:
            unet_key = DiffusionPipelineManager.get_unet_key(
                size, lora=lora_key, lycoris=lycoris_key, inversion=inversion_key
            )
            unet_plan = os.path.join(model_dir, "unet", unet_key, "engine.plan")
            unet_ready = os.path.exists(unet_plan)

            controlled_unet_key = DiffusionPipelineManager.get_controlled_unet_key(
                size, lora=lora_key, lycoris=lycoris_key, inversion=inversion_key
            )
            controlled_unet_plan = os.path.join(model_dir, "controlledunet", controlled_unet_key, "engine.plan")
            controlled_unet_ready = os.path.exists(controlled_unet_plan)

        ready = clip_ready and vae_ready
        if controlnet is not None or DiffusionPipelineManager.TENSORRT_ALWAYS_USE_CONTROLLED_UNET:
            ready = ready and controlled_unet_ready
        else:
            ready = ready and unet_ready

        return {
            "supported": tensorrt_is_supported,
            "xl": model_is_sdxl,
            "unet_ready": unet_ready,
            "controlled_unet_ready": controlled_unet_ready,
            "vae_ready": vae_ready,
            "clip_ready": clip_ready,
            "ready": ready,
        }

    def create_inpainting_checkpoint(self, source_checkpoint_path: str, target_checkpoint_path: str) -> None:
        """
        Creates an inpainting model by merging in the SD 1.5 inpainting model with a non inpainting model.
        """
        from enfugue.diffusion.util import ModelMerger

        try:
            merger = ModelMerger(
                self.check_download_checkpoint(DEFAULT_INPAINTING_MODEL),
                source_checkpoint_path,
                self.check_download_checkpoint(DEFAULT_MODEL),
                interpolation="add-difference",
            )
            merger.save(target_checkpoint_path)
        except Exception as ex:
            logger.error(
                f"Couldn't save merged checkpoint made from {source_checkpoint_path} to {target_checkpoint_path}: {ex}"
            )
            logger.error(traceback.format_exc())
            raise
        else:
            logger.info(f"Saved merged inpainting checkpoint at {target_checkpoint_path}")
