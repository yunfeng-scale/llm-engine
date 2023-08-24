"""
TODO figure out how to do: (or if we want to do it)
List model endpoint history: GET model-endpoints/<endpoint id>/history
Read model endpoint creation logs: GET model-endpoints/<endpoint id>/creation-logs
"""

import json
import math
import os
from dataclasses import asdict
from typing import Any, AsyncIterable, Dict, List, Optional
from uuid import uuid4

from model_engine_server.common.config import hmi_config
from model_engine_server.common.dtos.llms import (
    CompletionOutput,
    CompletionStreamOutput,
    CompletionStreamV1Request,
    CompletionStreamV1Response,
    CompletionSyncV1Request,
    CompletionSyncV1Response,
    CreateLLMModelEndpointV1Request,
    CreateLLMModelEndpointV1Response,
    GetLLMModelEndpointV1Response,
    ListLLMModelEndpointsV1Response,
    ModelDownloadRequest,
    ModelDownloadResponse,
    TokenOutput,
)
from model_engine_server.common.dtos.model_bundles import CreateModelBundleV2Request
from model_engine_server.common.dtos.model_endpoints import ModelEndpointOrderBy
from model_engine_server.common.dtos.tasks import EndpointPredictV1Request, TaskStatus
from model_engine_server.common.resource_limits import validate_resource_requests
from model_engine_server.core.auth.authentication_repository import User
from model_engine_server.core.domain_exceptions import (
    ObjectHasInvalidValueException,
    ObjectNotAuthorizedException,
    ObjectNotFoundException,
)
from model_engine_server.core.loggers import filename_wo_ext, make_logger
from model_engine_server.domain.entities import (
    LLMInferenceFramework,
    LLMMetadata,
    LLMSource,
    ModelBundle,
    ModelBundleFlavorType,
    ModelEndpoint,
    ModelEndpointType,
    Quantization,
    RunnableImageFlavor,
    StreamingEnhancedRunnableImageFlavor,
)
from model_engine_server.domain.exceptions import (
    EndpointLabelsException,
    EndpointUnsupportedInferenceTypeException,
)
from model_engine_server.domain.gateways.llm_artifact_gateway import LLMArtifactGateway
from model_engine_server.domain.repositories import ModelBundleRepository
from model_engine_server.domain.services import LLMModelEndpointService, ModelEndpointService
from model_engine_server.infra.gateways.filesystem_gateway import FilesystemGateway

from ...common.datadog_utils import add_trace_request_id
from ..authorization.live_authorization_module import LiveAuthorizationModule
from .model_bundle_use_cases import CreateModelBundleV2UseCase
from .model_endpoint_use_cases import (
    _handle_post_inference_hooks,
    model_endpoint_entity_to_get_model_endpoint_response,
    validate_billing_tags,
    validate_deployment_resources,
    validate_labels,
    validate_post_inference_hooks,
)

logger = make_logger(filename_wo_ext(__name__))

_SUPPORTED_MODEL_NAMES = {
    LLMInferenceFramework.DEEPSPEED: {
        "mpt-7b": "mosaicml/mpt-7b",
        "mpt-7b-instruct": "mosaicml/mpt-7b-instruct",
        "gpt-j-6b": "EleutherAI/gpt-j-6b",
        "gpt-j-6b-zh-en": "EleutherAI/gpt-j-6b",
        "gpt4all-j": "nomic-ai/gpt4all-j",
        "dolly-v2-12b": "databricks/dolly-v2-12b",
        "stablelm-tuned-7b": "StabilityAI/stablelm-tuned-alpha-7b",
        "flan-t5-xxl": "google/flan-t5-xxl",
        "llama-7b": "decapoda-research/llama-7b-hf",
        "vicuna-13b": "eachadea/vicuna-13b-1.1",
    },
    LLMInferenceFramework.TEXT_GENERATION_INFERENCE: {
        "mpt-7b": "mosaicml/mpt-7b",
        "mpt-7b-instruct": "mosaicml/mpt-7b-instruct",
        "flan-t5-xxl": "google/flan-t5-xxl",
        "llama-7b": "decapoda-research/llama-7b-hf",
        "llama-2-7b": "meta-llama/Llama-2-7b-hf",
        "llama-2-7b-chat": "meta-llama/Llama-2-7b-chat-hf",
        "llama-2-13b": "meta-llama/Llama-2-13b-hf",
        "llama-2-13b-chat": "meta-llama/Llama-2-13b-chat-hf",
        "llama-2-70b": "meta-llama/Llama-2-70b-hf",
        "llama-2-70b-chat": "meta-llama/Llama-2-70b-chat-hf",
        "falcon-7b": "tiiuae/falcon-7b",
        "falcon-7b-instruct": "tiiuae/falcon-7b-instruct",
        "falcon-40b": "tiiuae/falcon-40b",
        "falcon-40b-instruct": "tiiuae/falcon-40b-instruct",
    },
}


def _model_endpoint_entity_to_get_llm_model_endpoint_response(
    model_endpoint: ModelEndpoint,
) -> GetLLMModelEndpointV1Response:
    if model_endpoint.record.metadata is None or "_llm" not in model_endpoint.record.metadata:
        raise ObjectHasInvalidValueException(
            f"Can't translate model entity to response, endpoint {model_endpoint.record.id} does not have LLM metadata."
        )
    llm_metadata = model_endpoint.record.metadata.get("_llm", {})
    response = GetLLMModelEndpointV1Response(
        id=model_endpoint.record.id,
        name=model_endpoint.record.name,
        model_name=llm_metadata["model_name"],
        source=llm_metadata["source"],
        status=model_endpoint.record.status,
        inference_framework=llm_metadata["inference_framework"],
        inference_framework_image_tag=llm_metadata["inference_framework_image_tag"],
        num_shards=llm_metadata["num_shards"],
        quantize=llm_metadata.get("quantize"),
        spec=model_endpoint_entity_to_get_model_endpoint_response(model_endpoint),
    )
    return response


def validate_model_name(model_name: str, inference_framework: LLMInferenceFramework) -> None:
    if model_name not in _SUPPORTED_MODEL_NAMES[inference_framework]:
        raise ObjectHasInvalidValueException(
            f"Model name {model_name} is not supported for inference framework {inference_framework}."
        )


def validate_num_shards(
    num_shards: int, inference_framework: LLMInferenceFramework, gpus: int
) -> None:
    if inference_framework == LLMInferenceFramework.DEEPSPEED:
        if num_shards <= 1:
            raise ObjectHasInvalidValueException("DeepSpeed requires more than 1 GPU.")
        if num_shards != gpus:
            raise ObjectHasInvalidValueException(
                f"DeepSpeed requires num shard {num_shards} to be the same as number of GPUs {gpus}."
            )


class CreateLLMModelEndpointV1UseCase:
    def __init__(
        self,
        create_model_bundle_use_case: CreateModelBundleV2UseCase,
        model_bundle_repository: ModelBundleRepository,
        model_endpoint_service: ModelEndpointService,
    ):
        self.authz_module = LiveAuthorizationModule()
        self.create_model_bundle_use_case = create_model_bundle_use_case
        self.model_bundle_repository = model_bundle_repository
        self.model_endpoint_service = model_endpoint_service

    async def create_model_bundle(
        self,
        user: User,
        endpoint_name: str,
        model_name: str,
        source: LLMSource,
        framework: LLMInferenceFramework,
        framework_image_tag: str,
        endpoint_type: ModelEndpointType,
        num_shards: int,
        quantize: Optional[Quantization],
        checkpoint_path: Optional[str],
    ) -> ModelBundle:
        if source == LLMSource.HUGGING_FACE:
            if framework == LLMInferenceFramework.DEEPSPEED:
                bundle_id = await self.create_deepspeed_bundle(
                    user,
                    model_name,
                    framework_image_tag,
                    endpoint_type,
                    endpoint_name,
                )
            elif framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE:
                bundle_id = await self.create_text_generation_inference_bundle(
                    user,
                    model_name,
                    framework_image_tag,
                    endpoint_name,
                    num_shards,
                    quantize,
                    checkpoint_path,
                )
            else:
                raise ObjectHasInvalidValueException(
                    f"Framework {framework} is not supported for source {source}."
                )
        else:
            raise ObjectHasInvalidValueException(f"Source {source} is not supported.")

        model_bundle = await self.model_bundle_repository.get_model_bundle(bundle_id)
        if model_bundle is None:
            raise ObjectNotFoundException(f"Model bundle {bundle_id} was not found after creation.")
        return model_bundle

    async def create_text_generation_inference_bundle(
        self,
        user: User,
        model_name: str,
        framework_image_tag: str,
        endpoint_unique_name: str,
        num_shards: int,
        quantize: Optional[Quantization],
        checkpoint_path: Optional[str],
    ):
        command = []

        # TGI requires max_input_length < max_total_tokens
        max_input_length = 2047
        max_total_tokens = 2048
        if "llama-2" in model_name:
            max_input_length = 4095
            max_total_tokens = 4096

        if checkpoint_path is not None:
            if checkpoint_path.startswith("s3://"):
                base_path = checkpoint_path.split("/")[-1]
                final_weights_folder = "model_files"
                subcommands = []

                s5cmd = "s5cmd"
                # This is a hack for now to skip installing s5cmd for text-generation-inference:0.9.3-launch_s3,
                # which has s5cmd binary already baked in. Otherwise, install s5cmd if it's not already available
                if framework_image_tag != "0.9.3-launch_s3":
                    subcommands.append(
                        f"{s5cmd} > /dev/null || conda install -c conda-forge -y {s5cmd}"
                    )
                else:
                    s5cmd = "./s5cmd"

                if base_path.endswith(".tar"):
                    # If the checkpoint file is a tar file, extract it into final_weights_folder
                    subcommands.extend(
                        [
                            f"{s5cmd} cp {checkpoint_path} .",
                            f"mkdir -p {final_weights_folder}",
                            f"tar --no-same-owner -xf {base_path} -C {final_weights_folder}",
                        ]
                    )
                else:
                    subcommands.append(
                        f"{s5cmd} --numworkers 512 cp --concurrency 10 {os.path.join(checkpoint_path, '*')} {final_weights_folder}"
                    )

                subcommands.append(
                    f"text-generation-launcher --hostname :: --model-id ./{final_weights_folder}  --num-shard {num_shards} --port 5005 --max-input-length {max_input_length} --max-total-tokens {max_total_tokens}"
                )

                if quantize:
                    subcommands[-1] = subcommands[-1] + f" --quantize {quantize}"
                command = [
                    "/bin/bash",
                    "-c",
                    ";".join(subcommands),
                ]
            else:
                raise ObjectHasInvalidValueException(
                    f"Not able to load checkpoint path {checkpoint_path}."
                )
        else:
            hf_model_name = _SUPPORTED_MODEL_NAMES[LLMInferenceFramework.TEXT_GENERATION_INFERENCE][
                model_name
            ]

            command = [
                "text-generation-launcher",
                "--model-id",
                hf_model_name,
                "--num-shard",
                str(num_shards),
                "--port",
                "5005",
                "--hostname",
                "::",
                "--max-input-length",
                str(max_input_length),
                "--max-total-tokens",
                str(max_total_tokens),
            ]
            if quantize:
                command = command + [f"--quantize {quantize}"]

        return (
            await self.create_model_bundle_use_case.execute(
                user,
                CreateModelBundleV2Request(
                    name=endpoint_unique_name,
                    schema_location="TBA",
                    flavor=StreamingEnhancedRunnableImageFlavor(
                        flavor=ModelBundleFlavorType.STREAMING_ENHANCED_RUNNABLE_IMAGE,
                        repository=hmi_config.tgi_repository,
                        tag=framework_image_tag,
                        command=command,
                        streaming_command=command,
                        protocol="http",
                        readiness_initial_delay_seconds=10,
                        healthcheck_route="/health",
                        predict_route="/generate",
                        streaming_predict_route="/generate_stream",
                        env={},
                    ),
                    metadata={},
                ),
                do_auth_check=False,
                # Skip auth check because llm create endpoint is called as the user itself,
                # but the user isn't directly making the action. It should come from the fine tune
                # job.
            )
        ).model_bundle_id

    async def create_deepspeed_bundle(
        self,
        user: User,
        model_name: str,
        framework_image_tag: str,
        endpoint_type: ModelEndpointType,
        endpoint_unique_name: str,
    ):
        if endpoint_type == ModelEndpointType.STREAMING:
            command = [
                "dumb-init",
                "--",
                "ddtrace-run",
                "run-streamer",
                "--http",
                "production_threads",
                "--concurrency",
                "1",
                "--config",
                "/install/spellbook/inference/service--spellbook_streaming_inference.yaml",
            ]
            return (
                await self.create_model_bundle_use_case.execute(
                    user,
                    CreateModelBundleV2Request(
                        name=endpoint_unique_name,
                        schema_location="TBA",
                        flavor=StreamingEnhancedRunnableImageFlavor(
                            flavor=ModelBundleFlavorType.STREAMING_ENHANCED_RUNNABLE_IMAGE,
                            repository="instant-llm",  # TODO: let user choose repo
                            tag=framework_image_tag,
                            command=command,
                            streaming_command=command,
                            env={
                                "MODEL_NAME": model_name,
                            },
                            protocol="http",
                            readiness_initial_delay_seconds=60,
                        ),
                        metadata={},
                    ),
                    do_auth_check=False,
                )
            ).model_bundle_id
        else:
            return (
                await self.create_model_bundle_use_case.execute(
                    user,
                    CreateModelBundleV2Request(
                        name=endpoint_unique_name,
                        schema_location="TBA",
                        flavor=RunnableImageFlavor(
                            flavor=ModelBundleFlavorType.RUNNABLE_IMAGE,
                            repository="instant-llm",
                            tag=framework_image_tag,
                            command=[
                                "dumb-init",
                                "--",
                                "ddtrace-run",
                                "run-service",
                                "--http",
                                "production_threads",
                                "--concurrency",
                                "1",
                                "--config",
                                "/install/spellbook/inference/service--spellbook_inference.yaml",
                            ],
                            env={
                                "MODEL_NAME": model_name,
                            },
                            protocol="http",
                            readiness_initial_delay_seconds=1800,
                        ),
                        metadata={},
                    ),
                    do_auth_check=False,
                )
            ).model_bundle_id

    async def execute(
        self, user: User, request: CreateLLMModelEndpointV1Request
    ) -> CreateLLMModelEndpointV1Response:
        validate_deployment_resources(
            min_workers=request.min_workers,
            max_workers=request.max_workers,
            endpoint_type=request.endpoint_type,
        )
        if request.labels is None:
            raise EndpointLabelsException("Endpoint labels cannot be None!")
        validate_labels(request.labels)
        validate_billing_tags(request.billing_tags)
        validate_post_inference_hooks(user, request.post_inference_hooks)
        validate_model_name(request.model_name, request.inference_framework)
        validate_num_shards(request.num_shards, request.inference_framework, request.gpus)

        if request.inference_framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE:
            if request.endpoint_type != ModelEndpointType.STREAMING:
                raise ObjectHasInvalidValueException(
                    f"Creating endpoint type {str(request.endpoint_type)} is not allowed. Can only create streaming endpoints for text-generation-inference."
                )

        bundle = await self.create_model_bundle(
            user,
            endpoint_name=request.name,
            model_name=request.model_name,
            source=request.source,
            framework=request.inference_framework,
            framework_image_tag=request.inference_framework_image_tag,
            endpoint_type=request.endpoint_type,
            num_shards=request.num_shards,
            quantize=request.quantize,
            checkpoint_path=request.checkpoint_path,
        )
        validate_resource_requests(
            bundle=bundle,
            cpus=request.cpus,
            memory=request.memory,
            storage=request.storage,
            gpus=request.gpus,
            gpu_type=request.gpu_type,
        )

        prewarm = request.prewarm
        if prewarm is None:
            prewarm = True

        high_priority = request.high_priority
        if high_priority is None:
            high_priority = False

        aws_role = self.authz_module.get_aws_role_for_user(user)
        results_s3_bucket = self.authz_module.get_s3_bucket_for_user(user)

        request.metadata["_llm"] = asdict(
            LLMMetadata(
                model_name=request.model_name,
                source=request.source,
                inference_framework=request.inference_framework,
                inference_framework_image_tag=request.inference_framework_image_tag,
                num_shards=request.num_shards,
                quantize=request.quantize,
            )
        )

        model_endpoint_record = await self.model_endpoint_service.create_model_endpoint(
            name=request.name,
            created_by=user.user_id,
            model_bundle_id=bundle.id,
            endpoint_type=request.endpoint_type,
            metadata=request.metadata,
            post_inference_hooks=request.post_inference_hooks,
            child_fn_info=None,
            cpus=request.cpus,
            gpus=request.gpus,
            memory=request.memory,
            gpu_type=request.gpu_type,
            storage=request.storage,
            optimize_costs=bool(request.optimize_costs),
            min_workers=request.min_workers,
            max_workers=request.max_workers,
            per_worker=request.per_worker,
            labels=request.labels,
            aws_role=aws_role,
            results_s3_bucket=results_s3_bucket,
            prewarm=prewarm,
            high_priority=high_priority,
            owner=user.team_id,
            default_callback_url=request.default_callback_url,
            default_callback_auth=request.default_callback_auth,
            public_inference=request.public_inference,
        )
        _handle_post_inference_hooks(
            created_by=user.user_id,
            name=request.name,
            post_inference_hooks=request.post_inference_hooks,
        )

        return CreateLLMModelEndpointV1Response(
            endpoint_creation_task_id=model_endpoint_record.creation_task_id  # type: ignore
        )


class ListLLMModelEndpointsV1UseCase:
    """
    Use case for listing all LLM Model Endpoint of a given user and model endpoint name.
    Also include public_inference LLM endpoints.
    """

    def __init__(self, llm_model_endpoint_service: LLMModelEndpointService):
        self.llm_model_endpoint_service = llm_model_endpoint_service

    async def execute(
        self, user: User, name: Optional[str], order_by: Optional[ModelEndpointOrderBy]
    ) -> ListLLMModelEndpointsV1Response:
        """
        Runs the use case to list all Model Endpoints owned by the user with the given name.

        Args:
            user: The owner of the model endpoint(s).
            name: The name of the Model Endpoint(s).
            order_by: An optional argument to specify the output ordering of the model endpoints.

        Returns:
            A response object that contains the model endpoints.
        """
        model_endpoints = await self.llm_model_endpoint_service.list_llm_model_endpoints(
            owner=user.team_id, name=name, order_by=order_by
        )
        return ListLLMModelEndpointsV1Response(
            model_endpoints=[
                _model_endpoint_entity_to_get_llm_model_endpoint_response(m)
                for m in model_endpoints
            ]
        )


class GetLLMModelEndpointByNameV1UseCase:
    """
    Use case for getting an LLM Model Endpoint of a given user by name.
    """

    def __init__(self, llm_model_endpoint_service: LLMModelEndpointService):
        self.llm_model_endpoint_service = llm_model_endpoint_service
        self.authz_module = LiveAuthorizationModule()

    async def execute(self, user: User, model_endpoint_name: str) -> GetLLMModelEndpointV1Response:
        """
        Runs the use case to get the LLM endpoint with the given name.

        Args:
            user: The owner of the model endpoint.
            model_endpoint_name: The name of the model endpoint.

        Returns:
            A response object that contains the model endpoint.

        Raises:
            ObjectNotFoundException: If a model endpoint with the given name could not be found.
            ObjectNotAuthorizedException: If the owner does not own the model endpoint.
        """
        model_endpoint = await self.llm_model_endpoint_service.get_llm_model_endpoint(
            model_endpoint_name
        )
        if not model_endpoint:
            raise ObjectNotFoundException
        if not self.authz_module.check_access_read_owned_entity(
            user, model_endpoint.record
        ) and not self.authz_module.check_endpoint_public_inference_for_user(
            user, model_endpoint.record
        ):
            raise ObjectNotAuthorizedException
        return _model_endpoint_entity_to_get_llm_model_endpoint_response(model_endpoint)


class DeleteLLMModelEndpointByIdV1UseCase:
    pass


def deepspeed_result_to_tokens(result: Dict[str, Any]) -> List[TokenOutput]:
    tokens = []
    for i in range(len(result["token_probs"]["token_probs"])):
        tokens.append(
            TokenOutput(
                token=result["token_probs"]["tokens"][i],
                log_prob=math.log(result["token_probs"]["token_probs"][i]),
            )
        )
    return tokens


class CompletionSyncV1UseCase:
    """
    Use case for running a prompt completion on an LLM endpoint.
    """

    def __init__(
        self,
        model_endpoint_service: ModelEndpointService,
        llm_model_endpoint_service: LLMModelEndpointService,
    ):
        self.model_endpoint_service = model_endpoint_service
        self.llm_model_endpoint_service = llm_model_endpoint_service
        self.authz_module = LiveAuthorizationModule()

    def model_output_to_completion_output(
        self,
        model_output: Dict[str, Any],
        model_endpoint: ModelEndpoint,
        with_token_probs: Optional[bool],
    ) -> CompletionOutput:
        model_content = _model_endpoint_entity_to_get_llm_model_endpoint_response(model_endpoint)

        if model_content.inference_framework == LLMInferenceFramework.DEEPSPEED:
            completion_token_count = len(model_output["token_probs"]["tokens"])
            tokens = None
            if with_token_probs:
                tokens = deepspeed_result_to_tokens(model_output)
            return CompletionOutput(
                text=model_output["text"],
                num_completion_tokens=completion_token_count,
                tokens=tokens,
            )
        elif model_content.inference_framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE:
            try:
                tokens = None
                if with_token_probs:
                    tokens = [
                        TokenOutput(token=t["text"], log_prob=t["logprob"])
                        for t in model_output["details"]["tokens"]
                    ]
                return CompletionOutput(
                    text=model_output["generated_text"],
                    # len(model_output["details"]["prefill"]) does not return the correct value reliably
                    num_completion_tokens=model_output["details"]["generated_tokens"],
                    tokens=tokens,
                )
            except Exception as e:
                logger.exception(f"Error parsing text-generation-inference output {model_output}")
                raise e
        else:
            raise EndpointUnsupportedInferenceTypeException(
                f"Unsupported inference framework {model_content.inference_framework}"
            )

    async def execute(
        self, user: User, model_endpoint_name: str, request: CompletionSyncV1Request
    ) -> CompletionSyncV1Response:
        """
        Runs the use case to create a sync inference task.

        Args:
            user: The user who is creating the sync inference task.
            model_endpoint_name: The name of the model endpoint for the task.
            request: The body of the request to forward to the endpoint.

        Returns:
            A response object that contains the status and result of the task.

        Raises:
            ObjectNotFoundException: If a model endpoint with the given name could not be found.
            ObjectNotAuthorizedException: If the owner does not own the model endpoint.
        """

        request_id = str(uuid4())
        add_trace_request_id(request_id)

        model_endpoints = await self.llm_model_endpoint_service.list_llm_model_endpoints(
            owner=user.team_id, name=model_endpoint_name, order_by=None
        )

        if len(model_endpoints) == 0:
            raise ObjectNotFoundException

        if len(model_endpoints) > 1:
            raise ObjectHasInvalidValueException(
                f"Expected 1 LLM model endpoint for model name {model_endpoint_name}, got {len(model_endpoints)}"
            )

        model_endpoint = model_endpoints[0]

        if not self.authz_module.check_access_read_owned_entity(
            user, model_endpoint.record
        ) and not self.authz_module.check_endpoint_public_inference_for_user(
            user, model_endpoint.record
        ):
            raise ObjectNotAuthorizedException

        if (
            model_endpoint.record.endpoint_type is not ModelEndpointType.SYNC
            and model_endpoint.record.endpoint_type is not ModelEndpointType.STREAMING
        ):
            raise EndpointUnsupportedInferenceTypeException(
                f"Endpoint {model_endpoint_name} does not serve sync requests."
            )

        inference_gateway = self.model_endpoint_service.get_sync_model_endpoint_inference_gateway()
        endpoint_content = _model_endpoint_entity_to_get_llm_model_endpoint_response(model_endpoint)
        if endpoint_content.inference_framework == LLMInferenceFramework.DEEPSPEED:
            args: Any = {
                "prompts": [request.prompt],
                "token_probs": True,
                "generate_kwargs": {
                    "do_sample": True,
                    "temperature": request.temperature,
                    "max_new_tokens": request.max_new_tokens,
                },
                "serialize_results_as_string": False,
            }
            if request.stop_sequences is not None:
                # Deepspeed models only accepts one stop sequence
                args["stop_sequence"] = request.stop_sequences[0]

            inference_request = EndpointPredictV1Request(args=args)
            predict_result = await inference_gateway.predict(
                topic=model_endpoint.record.destination, predict_request=inference_request
            )

            if predict_result.status == TaskStatus.SUCCESS and predict_result.result is not None:
                return CompletionSyncV1Response(
                    request_id=request_id,
                    output=self.model_output_to_completion_output(
                        predict_result.result["result"][0],
                        model_endpoint,
                        request.return_token_log_probs,
                    ),
                )
            else:
                return CompletionSyncV1Response(
                    request_id=request_id,
                    output=None,
                )
        elif (
            endpoint_content.inference_framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE
        ):
            tgi_args: Any = {
                "inputs": request.prompt,
                "parameters": {
                    "max_new_tokens": request.max_new_tokens,
                    "decoder_input_details": True,
                },
            }
            if request.stop_sequences is not None:
                tgi_args["parameters"]["stop"] = request.stop_sequences
            if request.temperature > 0:
                tgi_args["parameters"]["temperature"] = request.temperature
                tgi_args["parameters"]["do_sample"] = True

            inference_request = EndpointPredictV1Request(args=tgi_args)
            predict_result = await inference_gateway.predict(
                topic=model_endpoint.record.destination, predict_request=inference_request
            )

            if predict_result.status != TaskStatus.SUCCESS or predict_result.result is None:
                return CompletionSyncV1Response(
                    request_id=request_id,
                    output=None,
                )

            output = json.loads(predict_result.result["result"])

            return CompletionSyncV1Response(
                request_id=request_id,
                output=self.model_output_to_completion_output(
                    output, model_endpoint, request.return_token_log_probs
                ),
            )
        else:
            raise EndpointUnsupportedInferenceTypeException(
                f"Unsupported inference framework {endpoint_content.inference_framework}"
            )


class CompletionStreamV1UseCase:
    """
    Use case for running a stream prompt completion on an LLM endpoint.
    """

    def __init__(
        self,
        model_endpoint_service: ModelEndpointService,
        llm_model_endpoint_service: LLMModelEndpointService,
    ):
        self.model_endpoint_service = model_endpoint_service
        self.llm_model_endpoint_service = llm_model_endpoint_service
        self.authz_module = LiveAuthorizationModule()

    async def execute(
        self, user: User, model_endpoint_name: str, request: CompletionStreamV1Request
    ) -> AsyncIterable[CompletionStreamV1Response]:
        """
        Runs the use case to create a stream inference task.

        Args:
            user: The user who is creating the stream inference task.
            model_endpoint_name: The name of the model endpoint for the task.
            request: The body of the request to forward to the endpoint.

        Returns:
            A response object that contains the status and result of the task.

        Raises:
            ObjectNotFoundException: If a model endpoint with the given name could not be found.
            ObjectNotAuthorizedException: If the owner does not own the model endpoint.
        """

        request_id = str(uuid4())
        add_trace_request_id(request_id)
        model_endpoints = await self.llm_model_endpoint_service.list_llm_model_endpoints(
            owner=user.team_id, name=model_endpoint_name, order_by=None
        )

        if len(model_endpoints) == 0:
            raise ObjectNotFoundException

        if len(model_endpoints) > 1:
            raise ObjectHasInvalidValueException(
                f"Expected 1 LLM model endpoint for model name {model_endpoint_name}, got {len(model_endpoints)}"
            )

        model_endpoint = model_endpoints[0]

        if not self.authz_module.check_access_read_owned_entity(
            user, model_endpoint.record
        ) and not self.authz_module.check_endpoint_public_inference_for_user(
            user, model_endpoint.record
        ):
            raise ObjectNotAuthorizedException

        if model_endpoint.record.endpoint_type != ModelEndpointType.STREAMING:
            raise EndpointUnsupportedInferenceTypeException(
                f"Endpoint {model_endpoint_name} is not a streaming endpoint."
            )

        inference_gateway = (
            self.model_endpoint_service.get_streaming_model_endpoint_inference_gateway()
        )

        model_content = _model_endpoint_entity_to_get_llm_model_endpoint_response(model_endpoint)

        args: Any = None
        if model_content.inference_framework == LLMInferenceFramework.DEEPSPEED:
            args = {
                "prompts": [request.prompt],
                "token_probs": True,
                "generate_kwargs": {
                    "do_sample": True,
                    "temperature": request.temperature,
                    "max_new_tokens": request.max_new_tokens,
                },
                "serialize_results_as_string": False,
            }
            if request.stop_sequences is not None:
                # Deepspeed models only accepts one stop sequence
                args["stop_sequence"] = request.stop_sequences[0]
        elif model_content.inference_framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE:
            args = {
                "inputs": request.prompt,
                "parameters": {
                    "max_new_tokens": request.max_new_tokens,
                },
            }
            if request.stop_sequences is not None:
                args["parameters"]["stop"] = request.stop_sequences
            if request.temperature > 0:
                args["parameters"]["temperature"] = request.temperature
                args["parameters"]["do_sample"] = True

        inference_request = EndpointPredictV1Request(args=args)

        predict_result = inference_gateway.streaming_predict(
            topic=model_endpoint.record.destination, predict_request=inference_request
        )

        num_completion_tokens = 0
        async for res in predict_result:
            result = res.result
            if model_content.inference_framework == LLMInferenceFramework.DEEPSPEED:
                if res.status == TaskStatus.SUCCESS and result is not None:
                    if "token" in result["result"]:
                        yield CompletionStreamV1Response(
                            request_id=request_id,
                            output=CompletionStreamOutput(
                                text=result["result"]["token"],
                                finished=False,
                                num_completion_tokens=None,
                            ),
                        )
                    else:
                        completion_token_count = len(
                            result["result"]["response"][0]["token_probs"]["tokens"]
                        )
                        yield CompletionStreamV1Response(
                            request_id=request_id,
                            output=CompletionStreamOutput(
                                text=result["result"]["response"][0]["text"],
                                finished=True,
                                num_completion_tokens=completion_token_count,
                            ),
                        )
                else:
                    yield CompletionStreamV1Response(
                        request_id=request_id,
                        output=None,
                    )
            elif (
                model_content.inference_framework == LLMInferenceFramework.TEXT_GENERATION_INFERENCE
            ):
                if res.status == TaskStatus.SUCCESS and result is not None:
                    if result["result"].get("generated_text") is not None:
                        finished = True
                    else:
                        finished = False

                    num_completion_tokens += 1

                    token = None
                    if request.return_token_log_probs:
                        token = TokenOutput(
                            token=result["result"]["token"]["text"],
                            log_prob=result["result"]["token"]["logprob"],
                        )
                    yield CompletionStreamV1Response(
                        request_id=request_id,
                        output=CompletionStreamOutput(
                            text=result["result"]["token"]["text"],
                            finished=finished,
                            num_completion_tokens=num_completion_tokens,
                            token=token,
                        ),
                    )
                else:
                    yield CompletionStreamV1Response(
                        request_id=request_id,
                        output=None,
                    )
            else:
                raise EndpointUnsupportedInferenceTypeException(
                    f"Unsupported inference framework {model_content.inference_framework}"
                )


class ModelDownloadV1UseCase:
    def __init__(
        self,
        filesystem_gateway: FilesystemGateway,
        model_endpoint_service: ModelEndpointService,
        llm_artifact_gateway: LLMArtifactGateway,
    ):
        self.filesystem_gateway = filesystem_gateway
        self.model_endpoint_service = model_endpoint_service
        self.llm_artifact_gateway = llm_artifact_gateway

    async def execute(self, user: User, request: ModelDownloadRequest) -> ModelDownloadResponse:
        model_endpoints = await self.model_endpoint_service.list_model_endpoints(
            owner=user.team_id, name=request.model_name, order_by=None
        )
        if len(model_endpoints) == 0:
            raise ObjectNotFoundException

        if len(model_endpoints) > 1:
            raise ObjectHasInvalidValueException(
                f"Expected 1 LLM model endpoint for model name {request.model_name}, got {len(model_endpoints)}"
            )
        model_files = self.llm_artifact_gateway.get_model_weights_urls(
            user.team_id, request.model_name
        )
        urls = {}
        for model_file in model_files:
            # don't want to make s3 bucket full keys public, so trim to just keep file name
            public_file_name = model_file.rsplit("/", 1)[-1]
            urls[public_file_name] = self.filesystem_gateway.generate_signed_url(model_file)
        return ModelDownloadResponse(urls=urls)