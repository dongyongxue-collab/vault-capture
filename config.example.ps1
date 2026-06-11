# Any OpenAI-compatible chat completions API can be used.
# Examples:
# - OpenAI:      https://api.openai.com/v1
# - DeepSeek:    https://api.deepseek.com
# - Moonshot:    https://api.moonshot.cn/v1
# - OpenRouter:  https://openrouter.ai/api/v1
# - Zhipu:       https://open.bigmodel.cn/api/paas/v4
$env:LLM_PROVIDER = "openai-compatible"
$env:LLM_API_BASE_URL = "https://api.example.com/v1"
$env:LLM_API_KEY = "your-llm-api-key"
$env:LLM_MODEL = "your-model-name"
$env:LLM_RESPONSE_FORMAT = "json_object"
$env:LLM_TEMPERATURE = "0"

# Optional provider-specific customization.
# $env:LLM_API_URL = "https://api.example.com/v1/chat/completions"
# $env:LLM_AUTH_HEADER = "Authorization"
# $env:LLM_AUTH_SCHEME = "Bearer" # use "none" if the provider expects the raw API key
# $env:LLM_EXTRA_HEADERS_JSON = '{"HTTP-Referer":"https://github.com/your/repo","X-Title":"Vault Capture"}'
# $env:LLM_EXTRA_BODY_JSON = '{}'

# Backward compatibility: older local configs using ZHIPU_API_KEY / ZHIPU_MODEL
# still work, but new configs should prefer LLM_*.

$env:OBSIDIAN_VAULT_PATH = "C:\path\to\your\Obsidian Vault"
$env:URL_CAPTURE_PORT = "8765"

$env:NOTION_API_TOKEN = "secret_xxx"
$env:NOTION_PARENT_PAGE_ID = "your-notion-parent-page-id"
