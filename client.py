#!/bin/which python3

# Modified version of Stability-AI SDK client.py. Changes:
#   - Calls cancel on ctrl-c to allow server to abort
#   - Supports setting ETA parameter
#   - Supports actually setting CLIP guidance strength
#   - Supports negative prompt by setting a prompt with negative weight
#   - Supports sending key to machines on local network over HTTP (not HTTPS)

import io
import logging
import mimetypes
import os
import pathlib
import random
import signal
import sys
import time
import uuid
from argparse import ArgumentParser, BooleanOptionalAction, Namespace
from typing import Any, Dict, Generator, List, Literal, Optional, Sequence, Tuple, Union

import grpc
import torch
from google.protobuf.json_format import MessageToJson
from PIL import Image, ImageOps
from safetensors.torch import safe_open

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    pass
else:
    load_dotenv()

# this is necessary because of how the auto-generated code constructs its imports
thisPath = pathlib.Path(__file__).parent.resolve()
genPath = thisPath / "gyre/generated"
sys.path.append(str(genPath))

import engines_pb2 as engines
import engines_pb2_grpc as engines_grpc
import generation_pb2 as generation
import generation_pb2_grpc as generation_grpc
import tensors_pb2 as tensors

from gyre.protobuf_safetensors import serialize_safetensor
from gyre.protobuf_tensors import serialize_tensor

logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)

SAMPLERS: Dict[str, int] = {
    "ddim": generation.SAMPLER_DDIM,
    "plms": generation.SAMPLER_DDPM,
    "k_euler": generation.SAMPLER_K_EULER,
    "k_euler_ancestral": generation.SAMPLER_K_EULER_ANCESTRAL,
    "k_heun": generation.SAMPLER_K_HEUN,
    "k_dpm_2": generation.SAMPLER_K_DPM_2,
    "k_dpm_2_ancestral": generation.SAMPLER_K_DPM_2_ANCESTRAL,
    "k_lms": generation.SAMPLER_K_LMS,
    "dpm_fast": generation.SAMPLER_DPM_FAST,
    "dpm_adaptive": generation.SAMPLER_DPM_ADAPTIVE,
    "dpmspp_1": generation.SAMPLER_DPMSOLVERPP_1ORDER,
    "dpmspp_2": generation.SAMPLER_DPMSOLVERPP_2ORDER,
    "dpmspp_3": generation.SAMPLER_DPMSOLVERPP_3ORDER,
    "dpmspp_2s_ancestral": generation.SAMPLER_DPMSOLVERPP_2S_ANCESTRAL,
    "dpmspp_sde": generation.SAMPLER_DPMSOLVERPP_SDE,
    "dpmspp_2m": generation.SAMPLER_DPMSOLVERPP_2M,
}

NOISE_TYPES: Dict[str, int] = {
    "normal": generation.SAMPLER_NOISE_NORMAL,
    "brownian": generation.SAMPLER_NOISE_BROWNIAN,
}


def get_sampler_from_str(s: str) -> generation.DiffusionSampler:
    """
    Convert a string to a DiffusionSampler enum.

    :param s: The string to convert.
    :return: The DiffusionSampler enum.
    """
    algorithm_key = s.lower().strip()
    algorithm = SAMPLERS.get(algorithm_key, None)
    if algorithm is None:
        raise ValueError(f"unknown sampler {s}")

    return algorithm


def get_noise_type_from_str(s: str) -> generation.SamplerNoiseType:
    noise_key = s.lower().strip()
    noise_type = NOISE_TYPES.get(noise_key, None)

    if noise_type is None:
        raise ValueError(f"unknown noise type {s}")

    return noise_type


def open_images(
    images: Union[
        Sequence[Tuple[str, generation.Artifact]],
        Generator[Tuple[str, generation.Artifact], None, None],
    ],
    verbose: bool = False,
) -> Generator[Tuple[str, generation.Artifact], None, None]:
    """
    Open the images from the filenames and Artifacts tuples.

    :param images: The tuples of Artifacts and associated images to open.
    :return:  A Generator of tuples of image filenames and Artifacts, intended
     for passthrough.
    """
    from PIL import Image

    for path, artifact in images:
        if artifact.type == generation.ARTIFACT_IMAGE:
            if verbose:
                logger.info(f"opening {path}")
            img = Image.open(io.BytesIO(artifact.binary))
            img.show()
        yield [path, artifact]


def image_to_prompt(
    im, init: bool = False, mask: bool = False, depth: bool = False, use_alpha=False
) -> generation.Prompt:
    if init and mask:
        raise ValueError("init and mask cannot both be True")

    if use_alpha:
        # Split into 3 channels
        r, g, b, a = im.split()
        # Recombine back to RGB image
        im = Image.merge("RGB", (a, a, a))
        im = ImageOps.invert(im)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)

    artifact_uuid = str(uuid.uuid4())

    type = generation.ARTIFACT_IMAGE
    if mask:
        type = generation.ARTIFACT_MASK
    if depth:
        type = generation.ARTIFACT_DEPTH

    return generation.Prompt(
        artifact=generation.Artifact(
            type=type, uuid=artifact_uuid, binary=buf.getvalue()
        ),
        parameters=generation.PromptParameters(init=init),
    )


def ref_to_prompt(ref_uuid, mask: bool = False, depth: bool = False):
    type = generation.ARTIFACT_IMAGE
    if mask:
        type = generation.ARTIFACT_MASK
    if depth:
        type = generation.ARTIFACT_DEPTH

    return generation.Prompt(
        artifact=generation.Artifact(
            type=type,
            ref=generation.ArtifactReference(
                uuid=ref_uuid, stage=generation.ARTIFACT_AFTER_ADJUSTMENTS
            ),
        )
    )


def lora_to_prompt(path, weights):
    safetensors = safe_open(path, framework="pt", device="cpu")

    lora = generation.Lora(lora=serialize_safetensor(safetensors))

    if weights:
        lora.weights.append(
            generation.LoraWeight(model_name="unet", weight=weights.pop(0))
        )

    if weights:
        lora.weights.append(
            generation.LoraWeight(model_name="text_encoder", weight=weights.pop(0))
        )

    return generation.Prompt(
        artifact=generation.Artifact(
            type=generation.ARTIFACT_LORA,
            lora=lora,
        ),
    )


def process_artifacts_from_answers(
    prefix: str,
    answers: Union[
        Generator[generation.Answer, None, None], Sequence[generation.Answer]
    ],
    write: bool = True,
    verbose: bool = False,
) -> Generator[Tuple[str, generation.Artifact], None, None]:
    """
    Process the Artifacts from the Answers.

    :param prefix: The prefix for the artifact filenames.
    :param answers: The Answers to process.
    :param write: Whether to write the artifacts to disk.
    :param verbose: Whether to print the artifact filenames.
    :return: A Generator of tuples of artifact filenames and Artifacts, intended
        for passthrough.
    """
    idx = 0
    for resp in answers:
        for artifact in resp.artifacts:
            artifact_p = f"{prefix}-{resp.request_id}-{resp.answer_id}-{idx}"
            if artifact.type == generation.ARTIFACT_IMAGE:
                ext = mimetypes.guess_extension(artifact.mime)
                contents = artifact.binary
            elif artifact.type == generation.ARTIFACT_CLASSIFICATIONS:
                ext = ".pb.json"
                contents = MessageToJson(artifact.classifier).encode("utf-8")
            elif artifact.type == generation.ARTIFACT_TEXT:
                ext = ".pb.json"
                contents = MessageToJson(artifact).encode("utf-8")
            else:
                ext = ".pb"
                contents = artifact.SerializeToString()
            out_p = f"{artifact_p}{ext}"
            if write:
                with open(out_p, "wb") as f:
                    f.write(bytes(contents))
                    if verbose:
                        artifact_t = generation.ArtifactType.Name(artifact.type)
                        logger.info(f"wrote {artifact_t} to {out_p}")
                        if artifact.finish_reason == generation.FILTER:
                            logger.info(f"{artifact_t} flagged as NSFW")

            yield [out_p, artifact]
            idx += 1


class StabilityInference:
    def __init__(
        self,
        host: str = "grpc.stability.ai:443",
        key: str = "",
        proto: Literal["grpc", "grpc-web"] = "grpc",
        engine: str = "stable-diffusion-v1-5",
        verbose: bool = False,
        wait_for_ready: bool = True,
    ):
        """
        Initialize the client.

        :param host: Host to connect to.
        :param key: Key to use for authentication.
        :param engine: Engine to use.
        :param verbose: Whether to print debug messages.
        :param wait_for_ready: Whether to wait for the server to be ready, or
            to fail immediately.
        """
        self.verbose = verbose
        self.engine = engine

        self.grpc_args = {}
        if proto == "grpc":
            self.grpc_args["wait_for_ready"] = wait_for_ready

        if verbose:
            logger.info(f"Opening channel to {host}")

        maxMsgLength = 30 * 1024 * 1024  # 30 MB

        channel_options = [
            ("grpc.max_message_length", maxMsgLength),
            ("grpc.max_send_message_length", maxMsgLength),
            ("grpc.max_receive_message_length", maxMsgLength),
        ]

        call_credentials = []

        if proto == "grpc-web":
            from gyre.sonora import client as sonora_client

            channel = sonora_client.insecure_web_channel(host)
        elif key:
            call_credentials.append(grpc.access_token_call_credentials(f"{key}"))

            if host.endswith("443"):
                channel_credentials = grpc.ssl_channel_credentials()
            else:
                print(
                    "Key provided but channel is not HTTPS - assuming a local network"
                )
                channel_credentials = grpc.local_channel_credentials()

            channel = grpc.secure_channel(
                host,
                grpc.composite_channel_credentials(
                    channel_credentials, *call_credentials
                ),
                options=channel_options,
            )
        else:
            channel = grpc.insecure_channel(host, options=channel_options)

        if verbose:
            logger.info(f"Channel opened to {host}")
        self.stub = generation_grpc.GenerationServiceStub(channel)
        self.engine_stub = engines_grpc.EnginesServiceStub(channel)

    def list_engines(self):
        request = engines.ListEnginesRequest()
        print(self.engine_stub.ListEngines(request))

    def generate(
        self,
        prompt: Union[str, List[str], generation.Prompt, List[generation.Prompt]],
        negative_prompt: str = None,
        init_image: Optional[Image.Image] = None,
        mask_image: Optional[Image.Image] = None,
        mask_from_image_alpha: bool = False,
        depth_image: Optional[Image.Image] = None,
        depth_from_image: bool = False,
        height: int = 512,
        width: int = 512,
        start_schedule: float = 1.0,
        end_schedule: float = 0.01,
        cfg_scale: float = 7.0,
        eta: float = 0.0,
        churn: float = None,
        churn_tmin: float = None,
        churn_tmax: float = None,
        sigma_min: float = None,
        sigma_max: float = None,
        karras_rho: float = None,
        noise_type: int = None,
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        steps: int = 50,
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        safety: bool = True,
        classifiers: Optional[generation.ClassifierParameters] = None,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: Optional[float] = None,
        guidance_prompt: Union[str, generation.Prompt] = None,
        guidance_models: List[str] = None,
        hires_fix: bool | None = None,
        hires_oos_fraction: float | None = None,
        tiling: bool = False,
        lora: list[tuple[str, list[float]]] | None = None,
        as_async=False,
    ) -> Generator[generation.Answer, None, None]:
        """
        Generate images from a prompt.

        :param prompt: Prompt to generate images from.
        :param init_image: Init image.
        :param mask_image: Mask image
        :param height: Height of the generated images.
        :param width: Width of the generated images.
        :param start_schedule: Start schedule for init image.
        :param end_schedule: End schedule for init image.
        :param cfg_scale: Scale of the configuration.
        :param sampler: Sampler to use.
        :param steps: Number of steps to take.
        :param seed: Seed for the random number generator.
        :param samples: Number of samples to generate.
        :param safety: DEPRECATED/UNUSED - Cannot be disabled.
        :param classifiers: DEPRECATED/UNUSED - Has no effect on image generation.
        :param guidance_preset: Guidance preset to use. See generation.GuidancePreset for supported values.
        :param guidance_cuts: Number of cuts to use for guidance.
        :param guidance_strength: Strength of the guidance. We recommend values in range [0.0,1.0]. A good default is 0.25
        :param guidance_prompt: Prompt to use for guidance, defaults to `prompt` argument (above) if not specified.
        :param guidance_models: Models to use for guidance.
        :return: Generator of Answer objects.
        """
        if (prompt is None) and (init_image is None):
            raise ValueError("prompt and/or init_image must be provided")

        if (mask_image is not None) and (init_image is None):
            raise ValueError(
                "If mask_image is provided, init_image must also be provided"
            )

        if not seed:
            seed = [random.randrange(0, 4294967295)]
        elif isinstance(seed, int):
            seed = [seed]
        else:
            seed = list(seed)

        prompts: List[generation.Prompt] = []
        if any(isinstance(prompt, t) for t in (str, generation.Prompt)):
            prompt = [prompt]
        for p in prompt:
            if isinstance(p, str):
                p = generation.Prompt(text=p)
            elif not isinstance(p, generation.Prompt):
                raise TypeError("prompt must be a string or generation.Prompt object")
            prompts.append(p)

        if negative_prompt:
            prompts += [
                generation.Prompt(
                    text=negative_prompt,
                    parameters=generation.PromptParameters(weight=-1),
                )
            ]

        sampler_parameters: dict[str, Any] = dict(cfg_scale=cfg_scale)

        if eta:
            sampler_parameters["eta"] = eta
        if noise_type:
            sampler_parameters["noise_type"] = noise_type

        if churn:
            churn_parameters = dict(churn=churn)

            if churn_tmin:
                churn_parameters["churn_tmin"] = churn_tmin
            if churn_tmax:
                churn_parameters["churn_tmax"] = churn_tmax

            sampler_parameters["churn"] = generation.ChurnSettings(**churn_parameters)

        sigma_parameters = {}

        if sigma_min:
            sigma_parameters["sigma_min"] = sigma_min
        if sigma_max:
            sigma_parameters["sigma_max"] = sigma_max
        if karras_rho:
            sigma_parameters["karras_rho"] = karras_rho

        sampler_parameters["sigma"] = generation.SigmaParameters(**sigma_parameters)

        step_parameters = dict(
            scaled_step=0, sampler=generation.SamplerParameters(**sampler_parameters)
        )

        # NB: Specifying schedule when there's no init image causes washed out results
        if init_image is not None:
            step_parameters["schedule"] = generation.ScheduleParameters(
                start=start_schedule,
                end=end_schedule,
            )
            init_image_prompt = image_to_prompt(init_image, init=True)
            prompts += [init_image_prompt]

            if mask_image is not None:
                prompts += [image_to_prompt(mask_image, mask=True)]

            elif mask_from_image_alpha:
                mask_prompt = ref_to_prompt(init_image_prompt.artifact.uuid, mask=True)
                mask_prompt.artifact.adjustments.append(
                    generation.ImageAdjustment(
                        channels=generation.ImageAdjustment_Channels(
                            r=generation.CHANNEL_A,
                            g=generation.CHANNEL_A,
                            b=generation.CHANNEL_A,
                            a=generation.CHANNEL_DISCARD,
                        )
                    )
                )
                mask_prompt.artifact.adjustments.append(
                    generation.ImageAdjustment(
                        invert=generation.ImageAdjustment_Invert()
                    )
                )

                prompts += [mask_prompt]

            if depth_image is not None:
                prompts += [image_to_prompt(depth_image, depth=True)]

            if depth_from_image:
                depth_prompt = ref_to_prompt(
                    init_image_prompt.artifact.uuid, depth=True
                )
                depth_prompt.artifact.adjustments.append(
                    generation.ImageAdjustment(depth=generation.ImageAdjustment_Depth())
                )
                prompts += [depth_prompt]

        if lora:
            for path, weights in lora:
                prompts += [lora_to_prompt(path, weights)]

        if guidance_prompt:
            if isinstance(guidance_prompt, str):
                guidance_prompt = generation.Prompt(text=guidance_prompt)
            elif not isinstance(guidance_prompt, generation.Prompt):
                raise ValueError("guidance_prompt must be a string or Prompt object")
        # if guidance_strength == 0.0:
        #    guidance_strength = None

        # Build our CLIP parameters
        if (
            guidance_preset is not generation.GUIDANCE_PRESET_NONE
            or guidance_strength is not None
        ):
            # to do: make it so user can override this
            # step_parameters['sampler']=None

            if guidance_models:
                guiders = [generation.Model(alias=model) for model in guidance_models]
            else:
                guiders = None

            if guidance_cuts:
                cutouts = generation.CutoutParameters(count=guidance_cuts)
            else:
                cutouts = None

            step_parameters["guidance"] = generation.GuidanceParameters(
                guidance_preset=guidance_preset,
                instances=[
                    generation.GuidanceInstanceParameters(
                        guidance_strength=guidance_strength,
                        models=guiders,
                        cutouts=cutouts,
                        prompt=guidance_prompt,
                    )
                ],
            )

        if hires_fix is None and hires_oos_fraction is not None:
            hires_fix = True

        hires = None

        if hires_fix is not None:
            hires_params: dict[str, bool | float] = dict(enable=hires_fix)
            if hires_oos_fraction is not None:
                hires_params["oos_fraction"] = hires_oos_fraction

            hires = generation.HiresFixParameters(**hires_params)

        image_parameters = generation.ImageParameters(
            transform=generation.TransformType(diffusion=sampler),
            height=height,
            width=width,
            seed=seed,
            steps=steps,
            samples=samples,
            parameters=[generation.StepParameter(**step_parameters)],
            hires=hires,
            tiling=tiling,
        )

        if as_async:
            return self.emit_async_request(
                prompt=prompts, image_parameters=image_parameters
            )
        else:
            return self.emit_request(prompt=prompts, image_parameters=image_parameters)

    # The motivation here is to facilitate constructing requests by passing protobuf objects directly.
    def emit_request(
        self,
        prompt: generation.Prompt,
        image_parameters: generation.ImageParameters,
        engine_id: str = None,
        request_id: str = None,
    ):
        if not request_id:
            request_id = str(uuid.uuid4())
        if not engine_id:
            engine_id = self.engine

        rq = generation.Request(
            engine_id=engine_id,
            request_id=request_id,
            prompt=prompt,
            image=image_parameters,
        )

        if self.verbose:
            logger.info("Sending request.")

        start = time.time()
        answers = self.stub.Generate(rq, **self.grpc_args)

        def cancel_request(unused_signum, unused_frame):
            print("Cancelling")
            answers.cancel()
            sys.exit(0)

        signal.signal(signal.SIGINT, cancel_request)

        for answer in answers:
            duration = time.time() - start
            if self.verbose:
                if len(answer.artifacts) > 0:
                    artifact_ts = [
                        generation.ArtifactType.Name(artifact.type)
                        for artifact in answer.artifacts
                    ]
                    logger.info(
                        f"Got {answer.answer_id} with {artifact_ts} in "
                        f"{duration:0.2f}s"
                    )
                else:
                    logger.info(
                        f"Got keepalive {answer.answer_id} in " f"{duration:0.2f}s"
                    )

            yield answer
            start = time.time()

    # The motivation here is to facilitate constructing requests by passing protobuf objects directly.
    def emit_async_request(
        self,
        prompt: generation.Prompt,
        image_parameters: generation.ImageParameters,
        engine_id: str = None,
        request_id: str = None,
    ):
        if not request_id:
            request_id = str(uuid.uuid4())
        if not engine_id:
            engine_id = self.engine

        rq = generation.Request(
            engine_id=engine_id,
            request_id=request_id,
            prompt=prompt,
            image=image_parameters,
        )

        if self.verbose:
            logger.info("Sending request.")

        start = time.time()
        handle = self.stub.AsyncGenerate(rq, **self.grpc_args)

        print(handle)

        def cancel_request(unused_signum, unused_frame):
            print("Cancelling")
            self.stub.AsyncCancel(handle)
            sys.exit(0)

        signal.signal(signal.SIGINT, cancel_request)

        while True:
            answers = self.stub.AsyncResult(handle)
            for answer in answers.answer:
                yield answer

            if answers.complete:
                print("Done")

            time.sleep(5)


if __name__ == "__main__":
    # Set up logging for output to console.
    fh = logging.StreamHandler()
    fh_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(filename)s(%(process)d) - %(message)s"
    )
    fh.setFormatter(fh_formatter)
    logger.addHandler(fh)

    STABILITY_HOST = os.getenv("STABILITY_HOST", "grpc.stability.ai:443")
    STABILITY_KEY = os.getenv("STABILITY_KEY", "")

    if not STABILITY_HOST:
        logger.warning("STABILITY_HOST environment variable needs to be set.")
        sys.exit(1)

    if not STABILITY_KEY:
        logger.warning(
            "STABILITY_KEY environment variable needs to be set. You may"
            " need to login to the Stability website to obtain the"
            " API key."
        )
        sys.exit(1)

    # CLI parsing
    parser = ArgumentParser()
    parser.add_argument(
        "--height", "-H", type=int, default=512, help="[512] height of image"
    )
    parser.add_argument(
        "--width", "-W", type=int, default=512, help="[512] width of image"
    )
    parser.add_argument(
        "--start_schedule",
        type=float,
        default=0.5,
        help="[0.5] start schedule for init image (must be greater than 0, 1 is full strength text prompt, no trace of image)",
    )
    parser.add_argument(
        "--end_schedule",
        type=float,
        default=0.01,
        help="[0.01] end schedule for init image",
    )
    parser.add_argument(
        "--cfg_scale", "-C", type=float, default=7.0, help="[7.0] CFG scale factor"
    )
    parser.add_argument(
        "--guidance_strength",
        "-G",
        type=float,
        default=None,
        help="[0.0] CLIP Guidance scale factor. We recommend values in range [0.0,1.0]. A good default is 0.25",
    )
    parser.add_argument(
        "--sampler",
        "-A",
        type=str,
        default="k_lms",
        help="[k_lms] (" + ", ".join(SAMPLERS.keys()) + ")",
    )
    parser.add_argument(
        "--eta",
        "-E",
        type=float,
        default=None,
        help="[None] ETA factor (for DDIM scheduler)",
    )
    parser.add_argument(
        "--churn",
        type=float,
        default=None,
        help="[None] churn factor (for Euler, Heun, DPM2 scheduler)",
    )
    parser.add_argument(
        "--churn_tmin",
        type=float,
        default=None,
        help="[None] churn sigma minimum (for Euler, Heun, DPM2 scheduler)",
    )
    parser.add_argument(
        "--churn_tmax",
        type=float,
        default=None,
        help="[None] churn sigma maximum (for Euler, Heun, DPM2 scheduler)",
    )
    parser.add_argument(
        "--sigma_min", type=float, default=None, help="[None] use this sigma min"
    )
    parser.add_argument(
        "--sigma_max", type=float, default=None, help="[None] use this sigma max"
    )
    parser.add_argument(
        "--karras_rho",
        type=float,
        default=None,
        help="[None] use Karras sigma schedule with this Rho",
    )
    parser.add_argument(
        "--noise_type",
        type=str,
        default="normal",
        help="[normal] (" + ", ".join(NOISE_TYPES.keys()) + ")",
    )
    parser.add_argument(
        "--steps", "-s", type=int, default=50, help="[50] number of steps"
    )
    parser.add_argument("--seed", "-S", type=int, default=0, help="random seed to use")
    parser.add_argument(
        "--prefix",
        "-p",
        type=str,
        default="generation_",
        help="output prefixes for artifacts",
    )
    parser.add_argument(
        "--no-store", action="store_true", help="do not write out artifacts"
    )
    parser.add_argument(
        "--num_samples", "-n", type=int, default=1, help="number of samples to generate"
    )
    parser.add_argument("--show", action="store_true", help="open artifacts using PIL")
    parser.add_argument(
        "--engine",
        "-e",
        type=str,
        help="engine to use for inference",
        default="stable-diffusion-v1-5",
    )
    parser.add_argument(
        "--init_image",
        "-i",
        type=str,
        help="Init image",
    )
    parser.add_argument(
        "--mask_image",
        "-m",
        type=str,
        help="Mask image",
    )
    parser.add_argument(
        "--mask_from_image_alpha",
        "-a",
        action="store_true",
        help="Get the mask from the image alpha channel, rather than a seperate image",
    )
    parser.add_argument(
        "--depth_image",
        type=str,
        help="Depth image",
    )
    parser.add_argument(
        "--depth_from_image",
        action="store_true",
        help="Inference the depth from the image",
    )
    parser.add_argument(
        "--negative_prompt",
        "-N",
        type=str,
        help="Negative Prompt",
    )
    parser.add_argument(
        "--hires_fix",
        action=BooleanOptionalAction,
        help="Enable or disable the hires fix for images above the 'natural' size of the model",
    )
    parser.add_argument(
        "--hires_oos_fraction",
        type=float,
        help="0..1, how out-of-square the area that's considered when doing a non-square hires fix should be. Low values risk more issues, high values zoom in more.",
    )
    parser.add_argument(
        "--tiling",
        action=BooleanOptionalAction,
        help="Enable or disable producing a tilable result",
    )
    parser.add_argument(
        "--lora",
        action="append",
        help="Add a (safetensor format) Lora. Either a path, or path:unet_weight or path:unet_weight:text_encode_weight (i.e. ./lora_weight.safetensors:0.5:0.5)",
    )
    parser.add_argument(
        "--list_engines",
        "-L",
        action="store_true",
        help="Print a list of the engines available on the server",
    )
    parser.add_argument(
        "--grpc_web",
        action="store_true",
        help="Use GRPC-WEB to connect to the server (instead of GRPC)",
    )
    parser.add_argument("--as_async", action="store_true", help="Run asyncronously")
    parser.add_argument("prompt", nargs="*")
    args = parser.parse_args()

    stability_api = StabilityInference(
        STABILITY_HOST,
        STABILITY_KEY,
        proto="grpc-web" if args.grpc_web else "grpc",
        engine=args.engine,
        verbose=True,
    )

    if args.list_engines:
        stability_api.list_engines()
        sys.exit(0)

    if not args.prompt and not args.init_image:
        logger.warning("prompt or init image must be provided")
        parser.print_help()
        sys.exit(1)
    else:
        args.prompt = " ".join(args.prompt)

    if args.init_image:
        args.init_image = Image.open(args.init_image)

    if args.mask_image:
        args.mask_image = Image.open(args.mask_image)

    if args.depth_image:
        args.depth_image = Image.open(args.depth_image)

    lora = []
    if args.lora:
        for path in args.lora:
            path, *weights = path.split(":")
            weights = [float(weight) for weight in weights]
            lora.append((path, weights))

    request = {
        "negative_prompt": args.negative_prompt,
        "height": args.height,
        "width": args.width,
        "start_schedule": args.start_schedule,
        "end_schedule": args.end_schedule,
        "cfg_scale": args.cfg_scale,
        "guidance_strength": args.guidance_strength,
        "sampler": get_sampler_from_str(args.sampler),
        "eta": args.eta,
        "churn": args.churn,
        "churn_tmin": args.churn_tmin,
        "churn_tmax": args.churn_tmax,
        "sigma_min": args.sigma_min,
        "sigma_max": args.sigma_max,
        "karras_rho": args.karras_rho,
        "noise_type": get_noise_type_from_str(args.noise_type),
        "steps": args.steps,
        "seed": args.seed,
        "samples": args.num_samples,
        "init_image": args.init_image,
        "mask_image": args.mask_image,
        "mask_from_image_alpha": args.mask_from_image_alpha,
        "depth_image": args.depth_image,
        "depth_from_image": args.depth_from_image,
        "hires_fix": args.hires_fix,
        "hires_oos_fraction": args.hires_oos_fraction,
        "tiling": args.tiling,
        "lora": lora,
        "as_async": args.as_async,
    }

    answers = stability_api.generate(args.prompt, **request)
    artifacts = process_artifacts_from_answers(
        args.prefix, answers, write=not args.no_store, verbose=True
    )
    if args.show:
        for artifact in open_images(artifacts, verbose=True):
            pass
    else:
        for artifact in artifacts:
            pass
