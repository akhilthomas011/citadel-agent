# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Client code for calling models through a Citadel gateway.

Snippets follow the gateway's verified calling pattern: the subscription key travels in the
``api-key`` header (read from Key Vault at runtime, never embedded), OpenAI SDKs get a placeholder
API key (or the JWT when the contract requires one), and an ``x-app-id`` header attributes usage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Framework = Literal[
    "openai-python",
    "azure-openai-python",
    "agent-framework",
    "langchain",
    "semantic-kernel",
    "curl",
    "powershell",
    "openai-javascript",
]
FRAMEWORKS: tuple[str, ...] = (
    "openai-python",
    "azure-openai-python",
    "agent-framework",
    "langchain",
    "semantic-kernel",
    "curl",
    "powershell",
    "openai-javascript",
)
ModelKind = Literal["chat", "embeddings", "images"]
AZURE_OPENAI_API_VERSION = "2024-10-21"
PROMPT = "Say hello in one short sentence."


def model_kind(name: str) -> ModelKind:
    lowered = name.lower()
    if "embed" in lowered:
        return "embeddings"
    if any(marker in lowered for marker in ("image", "flux", "dall-e", "dalle")):
        return "images"
    return "chat"


@dataclass(frozen=True)
class CodeRequest:
    framework: str
    gateway_url: str
    model: str
    key_secret: str
    key_vault: str | None = None
    jwt_scope: str | None = None
    app_id: str = "my-app"

    @property
    def kind(self) -> ModelKind:
        return model_kind(self.model)

    @property
    def v1_url(self) -> str:
        return self.gateway_url.rstrip("/") + "/unified-ai/v1"


@dataclass(frozen=True)
class GeneratedCode:
    language: str
    code: str
    packages: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class CodegenError(ValueError):
    """Raised for unsupported framework and model combinations."""


def _python_key(request: CodeRequest) -> str:
    vault = request.key_vault or "<key-vault-name>"
    lines = [
        "import os",
        "",
        "from azure.identity import DefaultAzureCredential",
        "from azure.keyvault.secrets import SecretClient",
        "",
        "credential = DefaultAzureCredential()",
        "# The subscription key of your access contract, read from Key Vault (or CITADEL_API_KEY).",
        'api_key = os.environ.get("CITADEL_API_KEY") or SecretClient(',
        f'    vault_url="https://{vault}.vault.azure.net", credential=credential',
        f').get_secret("{request.key_secret}").value',
    ]
    if request.jwt_scope:
        lines += [
            "# This contract also requires an Entra ID token for the gateway.",
            f'token = credential.get_token("{request.jwt_scope}").token',
        ]
    return "\n".join(lines)


def _python_call(request: CodeRequest, client: str = "client") -> str:
    if request.kind == "embeddings":
        return (
            f'result = {client}.embeddings.create(model="{request.model}", input=["Citadel gateway"])\n'
            "print(len(result.data[0].embedding))"
        )
    if request.kind == "images":
        return (
            f'result = {client}.images.generate(model="{request.model}", prompt="A lighthouse at dawn", '
            'size="1024x1024")\n'
            "print(result.data[0].b64_json[:40] if result.data[0].b64_json else result.data[0].url)"
        )
    return (
        f"response = {client}.chat.completions.create(\n"
        f'    model="{request.model}",\n'
        f'    messages=[{{"role": "user", "content": "{PROMPT}"}}],\n'
        ")\n"
        "print(response.choices[0].message.content)"
    )


def _openai_python(request: CodeRequest) -> GeneratedCode:
    api_key = "token" if request.jwt_scope else '"unused"'
    code = "\n".join(
        [
            _python_key(request),
            "",
            "from openai import OpenAI",
            "",
            "client = OpenAI(",
            f'    base_url="{request.v1_url}",',
            f"    api_key={api_key},  # the gateway reads the subscription key from the api-key header",
            f'    default_headers={{"api-key": api_key, "x-app-id": "{request.app_id}"}},',
            ")",
            _python_call(request),
        ]
    )
    return GeneratedCode("python", code, ("openai", "azure-identity", "azure-keyvault-secrets"))


def _azure_openai_python(request: CodeRequest) -> GeneratedCode:
    if request.jwt_scope:
        auth = [
            "from azure.identity import get_bearer_token_provider",
            "",
            "client = AzureOpenAI(",
            f'    azure_endpoint="{request.gateway_url.rstrip("/")}",',
            f'    azure_ad_token_provider=get_bearer_token_provider(credential, "{request.jwt_scope}"),',
            f'    api_version="{AZURE_OPENAI_API_VERSION}",',
            f'    default_headers={{"api-key": api_key, "x-app-id": "{request.app_id}"}},',
            ")",
        ]
    else:
        auth = [
            "client = AzureOpenAI(",
            f'    azure_endpoint="{request.gateway_url.rstrip("/")}",',
            "    api_key=api_key,",
            f'    api_version="{AZURE_OPENAI_API_VERSION}",',
            f'    default_headers={{"x-app-id": "{request.app_id}"}},',
            ")",
        ]
    code = "\n".join(
        [_python_key(request), "", "from openai import AzureOpenAI", "", *auth, _python_call(request)]
    )
    notes = (
        "This calls /openai/deployments/<model>; the access contract must include the azure-openai-api API.",
    )
    return GeneratedCode("python", code, ("openai", "azure-identity", "azure-keyvault-secrets"), notes)


def _agent_framework(request: CodeRequest) -> GeneratedCode:
    if request.kind != "chat":
        raise CodegenError("Agent Framework snippets are for chat models")
    api_key = "token" if request.jwt_scope else '"unused"'
    code = "\n".join(
        [
            "import asyncio",
            "",
            _python_key(request),
            "",
            "from agent_framework import Agent",
            "from agent_framework.openai import OpenAIChatCompletionClient",
            "",
            "client = OpenAIChatCompletionClient(",
            f'    model="{request.model}",',
            f"    api_key={api_key},",
            f'    base_url="{request.v1_url}",',
            f'    default_headers={{"api-key": api_key, "x-app-id": "{request.app_id}"}},',
            ")",
            'agent = Agent(client=client, instructions="You are a helpful assistant.")',
            f'print(asyncio.run(agent.run("{PROMPT}")).text)',
        ]
    )
    notes = ("Chat Completions works with pooled backends; avoid the stateful Responses client there.",)
    return GeneratedCode(
        "python", code, ("agent-framework", "azure-identity", "azure-keyvault-secrets"), notes
    )


def _langchain(request: CodeRequest) -> GeneratedCode:
    api_key = "token" if request.jwt_scope else '"unused"'
    common = [
        f'    base_url="{request.v1_url}",',
        f"    api_key={api_key},",
        f'    default_headers={{"api-key": api_key, "x-app-id": "{request.app_id}"}},',
    ]
    if request.kind == "embeddings":
        body = [
            "from langchain_openai import OpenAIEmbeddings",
            "",
            "embeddings = OpenAIEmbeddings(",
            f'    model="{request.model}",',
            *common,
            "    check_embedding_ctx_length=False,  # send text, not token arrays",
            ")",
            'print(len(embeddings.embed_query("Citadel gateway")))',
        ]
    elif request.kind == "chat":
        body = [
            "from langchain_openai import ChatOpenAI",
            "",
            "llm = ChatOpenAI(",
            f'    model="{request.model}",',
            *common,
            ")",
            f'print(llm.invoke("{PROMPT}").content)',
        ]
    else:
        raise CodegenError("LangChain snippets are for chat and embedding models")
    code = "\n".join([_python_key(request), "", *body])
    return GeneratedCode("python", code, ("langchain-openai", "azure-identity", "azure-keyvault-secrets"))


def _semantic_kernel(request: CodeRequest) -> GeneratedCode:
    if request.kind == "images":
        raise CodegenError("Semantic Kernel snippets are for chat and embedding models")
    api_key = "token" if request.jwt_scope else '"unused"'
    client = [
        "from openai import AsyncOpenAI",
        "",
        "client = AsyncOpenAI(",
        f'    base_url="{request.v1_url}",',
        f"    api_key={api_key},",
        f'    default_headers={{"api-key": api_key, "x-app-id": "{request.app_id}"}},',
        ")",
    ]
    if request.kind == "embeddings":
        body = [
            "from semantic_kernel.connectors.ai.open_ai import OpenAITextEmbedding",
            "",
            f'service = OpenAITextEmbedding(ai_model_id="{request.model}", async_client=client)',
            "",
            "async def main() -> None:",
            '    vectors = await service.generate_embeddings(["Citadel gateway"])',
            "    print(len(vectors[0]))",
        ]
    else:
        body = [
            "from semantic_kernel.connectors.ai.open_ai import (",
            "    OpenAIChatCompletion,",
            "    OpenAIChatPromptExecutionSettings,",
            ")",
            "from semantic_kernel.contents import ChatHistory",
            "",
            f'service = OpenAIChatCompletion(ai_model_id="{request.model}", async_client=client)',
            "",
            "async def main() -> None:",
            "    history = ChatHistory()",
            f'    history.add_user_message("{PROMPT}")',
            "    reply = await service.get_chat_message_content(",
            "        chat_history=history, settings=OpenAIChatPromptExecutionSettings()",
            "    )",
            "    print(reply)",
        ]
    code = "\n".join(
        ["import asyncio", "", _python_key(request), "", *client, "", *body, "", "asyncio.run(main())"]
    )
    notes = ("Chat Completions works with pooled backends; avoid the stateful Responses connector there.",)
    return GeneratedCode(
        "python", code, ("semantic-kernel", "azure-identity", "azure-keyvault-secrets"), notes
    )


def _payload(request: CodeRequest) -> tuple[str, str]:
    if request.kind == "embeddings":
        return "embeddings", f'{{"model": "{request.model}", "input": ["Citadel gateway"]}}'
    if request.kind == "images":
        return (
            "images/generations",
            f'{{"model": "{request.model}", "prompt": "A lighthouse at dawn", "size": "1024x1024"}}',
        )
    return (
        "chat/completions",
        f'{{"model": "{request.model}", "messages": [{{"role": "user", "content": "{PROMPT}"}}]}}',
    )


def _curl(request: CodeRequest) -> GeneratedCode:
    path, payload = _payload(request)
    vault = request.key_vault or "<key-vault-name>"
    lines = [
        f'API_KEY="${{CITADEL_API_KEY:-$(az keyvault secret show --vault-name {vault} '
        f'--name {request.key_secret} --query value -o tsv)}}"',
    ]
    auth = ""
    if request.jwt_scope:
        lines.append(
            f'TOKEN=$(az account get-access-token --scope "{request.jwt_scope}" --query accessToken -o tsv)'
        )
        auth = ' -H "Authorization: Bearer $TOKEN"'
    lines.append(
        f'curl -sS "{request.v1_url}/{path}" -H "api-key: $API_KEY"{auth} -H "x-app-id: {request.app_id}" '
        f"-H \"Content-Type: application/json\" -d '{payload}'"
    )
    return GeneratedCode("bash", "\n".join(lines), ("Azure CLI",))


def _powershell(request: CodeRequest) -> GeneratedCode:
    path, payload = _payload(request)
    vault = request.key_vault or "<key-vault-name>"
    lines = [
        "$apiKey = $env:CITADEL_API_KEY",
        "if (-not $apiKey) {",
        f"    $apiKey = az keyvault secret show --vault-name {vault} --name {request.key_secret} "
        "--query value -o tsv",
        "}",
        f'$headers = @{{ "api-key" = $apiKey; "x-app-id" = "{request.app_id}" }}',
    ]
    if request.jwt_scope:
        lines += [
            f'$token = az account get-access-token --scope "{request.jwt_scope}" --query accessToken -o tsv',
            '$headers["Authorization"] = "Bearer $token"',
        ]
    lines += [
        f"$body = '{payload}'",
        f'$response = Invoke-RestMethod -Method Post -Uri "{request.v1_url}/{path}" -Headers $headers '
        '-ContentType "application/json" -Body $body',
    ]
    if request.kind == "chat":
        lines.append("$response.choices[0].message.content")
    elif request.kind == "embeddings":
        lines.append("$response.data[0].embedding.Count")
    else:
        lines.append("$response.data[0].b64_json.Substring(0, 40)")
    return GeneratedCode("powershell", "\n".join(lines), ("Azure CLI",))


def _openai_javascript(request: CodeRequest) -> GeneratedCode:
    if request.kind != "chat":
        raise CodegenError("JavaScript snippets are for chat models")
    api_key = "process.env.CITADEL_TOKEN" if request.jwt_scope else '"unused"'
    code = "\n".join(
        [
            'import OpenAI from "openai";',
            "",
            "// Read the subscription key from Key Vault (for example with @azure/keyvault-secrets)",
            "// or provide it in CITADEL_API_KEY.",
            "const client = new OpenAI({",
            f'  baseURL: "{request.v1_url}",',
            f"  apiKey: {api_key},",
            '  defaultHeaders: { "api-key": process.env.CITADEL_API_KEY, '
            f'"x-app-id": "{request.app_id}" }},',
            "});",
            "const response = await client.chat.completions.create({",
            f'  model: "{request.model}",',
            f'  messages: [{{ role: "user", content: "{PROMPT}" }}],',
            "});",
            "console.log(response.choices[0].message.content);",
        ]
    )
    return GeneratedCode("javascript", code, ("openai",))


_GENERATORS = {
    "openai-python": _openai_python,
    "azure-openai-python": _azure_openai_python,
    "agent-framework": _agent_framework,
    "langchain": _langchain,
    "semantic-kernel": _semantic_kernel,
    "curl": _curl,
    "powershell": _powershell,
    "openai-javascript": _openai_javascript,
}


def generate(request: CodeRequest) -> GeneratedCode:
    """Client code for ``request``."""
    generator = _GENERATORS.get(request.framework)
    if generator is None:
        raise CodegenError(f"Unknown framework '{request.framework}'. Choose one of: {', '.join(FRAMEWORKS)}")
    return generator(request)
