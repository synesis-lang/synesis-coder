###########################
# Processar arquivo único
# python interview_processor.py entrevista1.txt
#
# Processar pasta inteira
# python interview_processor.py --folder interviews/
#
# Batch com créditos mínimos de $2.00
# python interview_processor.py --folder interviews/ --min-credits 2.0
#
# Batch sem verificação de créditos
# python interview_processor.py --folder interviews/ --no-credit-check
#
# Batch com pasta de saída diferente
# python interview_processor.py --folder interviews/ --output-folder output/
###########################

import anthropic
import asyncio
from tqdm import tqdm
import argparse
import logging
import sys
from typing import List, Dict, Tuple, Optional, Any
import os
import re
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import nltk
import time
import toml
from collections import deque
import hashlib
import yaml
import requests
from glob import glob
from datetime import datetime

# Load configuration from config.toml
config = toml.load("config.toml")

# Configuration constants
AI_model = config["abstract_processor"]["AI_model"]
DEFAULT_API_KEY = config["abstract_processor"]["api_key"]
DEFAULT_CONCURRENCY = int(config["abstract_processor"]["concurrent"])
DEFAULT_RETRIES = int(config["abstract_processor"]["retries"])
DEFAULT_BATCH_SIZE = int(config["abstract_processor"]["batch_size"])
DEFAULT_SCAN_MODE = config["abstract_processor"].get("scan_mode", "single")
log_file = config["abstract_processor"].get("log_file", "interview_processor.log")

# Batch processing configuration
DEFAULT_INPUT_FOLDER = config["abstract_processor"].get("input_folder", "interviews")
DEFAULT_MIN_CREDITS_USD = float(config["abstract_processor"].get("min_credits_usd", 1.00))
DEFAULT_CREDITS_CHECK_ENABLED = config["abstract_processor"].get("credits_check_enabled", True)

print("Preparing data! Please, be patient...")

# Prompt base
system_prompt = config["prompts"]["system_prompt"]

# Create log directory if it doesn't exist
log_dir = os.path.dirname(log_file)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

def _configure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                try:
                    stream.reconfigure(errors="backslashreplace")
                except Exception:
                    pass

_configure_utf8_streams()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Ensure NLTK punkt data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    logger.info("Downloading NLTK punkt data")
    nltk.download('punkt')

class InterviewProcessor:
    """Handles processing of interview transcripts with enhanced text handling and prompt caching."""

    SCAN_SCOPE_EXPLORATORIOS = "fatores_exploratorios"
    SCAN_SCOPE_CLASSIFICATORIOS = "fatores_classificatorios"
    SCAN_SCOPES = (SCAN_SCOPE_EXPLORATORIOS, SCAN_SCOPE_CLASSIFICATORIOS)

    # Claude model token limits (context window)
    MODEL_TOKEN_LIMITS = {
        "claude-sonnet-4-5-20250929": 200000,
        "claude-opus-4-20250514": 200000,
        "claude-3-5-sonnet-20241022": 200000,
        "claude-3-opus-20240229": 200000,
        "claude-3-sonnet-20240229": 200000,
        "claude-3-haiku-20240307": 200000,
    }

    # Minimum tokens for prompt caching (1024 tokens minimum for cache_control)
    MIN_CACHE_TOKENS = 1024

    # Maximum output tokens by model
    MAX_OUTPUT_TOKENS = {
        "claude-sonnet-4-5-20250929": 16384,
        "claude-opus-4-20250514": 16384,
        "claude-3-5-sonnet-20241022": 8192,
        "claude-3-opus-20240229": 4096,
        "claude-3-sonnet-20240229": 4096,
        "claude-3-haiku-20240307": 4096,
    }

    # Pricing per 1M tokens (USD) - Claude Sonnet 4.5
    MODEL_PRICING = {
        "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
        "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
        "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
        "claude-3-opus-20240229": {"input": 15.00, "output": 75.00},
        "claude-3-sonnet-20240229": {"input": 3.00, "output": 15.00},
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    }

    def __init__(self, api_key: str, output_file: str, max_concurrent: int, max_retries: int,
                 scan_mode: str = "single", min_credits_usd: float = 1.00,
                 credits_check_enabled: bool = True):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.api_key = api_key
        self.output_file = output_file
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.scan_mode = scan_mode
        self.scan_scopes = self._resolve_scan_scopes(scan_mode)

        # Credit control settings
        self.min_credits_usd = min_credits_usd
        self.credits_check_enabled = credits_check_enabled
        self.total_cost_session = 0.0
        self.initial_credits = None
        self.files_processed = 0
        self.files_failed = 0

        # Use deque for efficient rate limiting (optimized for Tier 1)
        self.recent_calls = deque()
        self.rate_window = 60
        self.max_rpm = 50  # Tier 1 allows 50 RPM
        self.input_tokens_used = deque()
        self.output_tokens_used = deque()
        self.max_input_tokens_per_minute = 40000  # Tier 1 allows 40k input TPM
        self.max_output_tokens_per_minute = 8000  # Tier 1 allows 8k output TPM

        # Cache for token estimation
        self._token_cache = {}

        # Create static prompt parts and cache the base prompt
        self.static_prompt_parts = self._create_static_prompt_parts()
        self._variable_dict_text = self.static_prompt_parts.get("variable_dictionary", "")
        self._variable_dict_parse_failed = False
        self._variable_dict_data = self._parse_variable_dictionary_yaml(self._variable_dict_text)
        self._variable_dictionary_by_scope = {
            scope: self._build_variable_dictionary_subset(scope)
            for scope in self.SCAN_SCOPES
        }
        self._cached_base_prompt = self._build_base_prompt_template()

        # Prompt caching: pre-compute static content for cache_control
        self._cached_system_prompt = self._build_cached_system_prompt()
        self._cached_user_prefix = self._build_cached_user_prefix()
        self._cached_user_prefix_by_scope = {
            scope: self._build_cached_user_prefix(scope)
            for scope in self.SCAN_SCOPES
        }

        # Token limits for the current model
        self.model_token_limit = self.MODEL_TOKEN_LIMITS.get(AI_model, 200000)
        self.max_output_limit = self.MAX_OUTPUT_TOKENS.get(AI_model, 8192)

        # Estimate static prompt tokens once
        self._static_prompt_tokens = self.estimate_tokens(
            self._cached_system_prompt + self._cached_user_prefix
        )
        self._static_prompt_tokens_by_scope = {
            scope: self.estimate_tokens(self._cached_system_prompt + prefix)
            for scope, prefix in self._cached_user_prefix_by_scope.items()
        }
        self._static_prompt_tokens_max = max(
            [self._static_prompt_tokens] + list(self._static_prompt_tokens_by_scope.values())
        )
        logger.info(f"Static prompt tokens (cached): {self._static_prompt_tokens}")
        for scope, tokens in self._static_prompt_tokens_by_scope.items():
            logger.info(f"Static prompt tokens (cached, {scope}): {tokens}")
        logger.info(f"Scan mode: {self.scan_mode}")

        # Buffer for batch file writes
        self.output_buffer = []
        self.buffer_size = 20  # Write every 20 results

        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("")

    def _create_static_prompt_parts(self) -> Dict[str, str]:
        return {
            "task": config["prompts"]["task"],
            "instructions": config["prompts"]["instructions"],
            "variable_dictionary": config["prompts"]["variable_dictionary"],
            "output_format": config["prompts"]["output_format"]
        }

    def get_api_credits(self) -> Tuple[bool, Optional[float], str]:
        """
        Check API credits/balance using Anthropic's billing API.

        Returns:
            Tuple of (success, balance_usd, message)
        """
        try:
            # Try to get organization billing info via Admin API
            # Note: This requires admin API access which may not be available
            # Fallback: Make a minimal test call to verify API access
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }

            # Attempt to get billing information (if available)
            # This endpoint may not be publicly documented
            try:
                response = requests.get(
                    "https://api.anthropic.com/v1/organizations/billing",
                    headers=headers,
                    timeout=30
                )
                if response.status_code == 200:
                    data = response.json()
                    balance = data.get("balance", data.get("credits", None))
                    if balance is not None:
                        return True, float(balance), f"Balance: ${balance:.2f} USD"
            except Exception:
                pass

            # Fallback: Make a minimal API test call to verify the API key works
            try:
                test_response = self.client.messages.create(
                    model=AI_model,
                    max_tokens=5,
                    messages=[{"role": "user", "content": "Hi"}]
                )
                # API call succeeded - we have at least some credits
                input_tokens = test_response.usage.input_tokens
                output_tokens = test_response.usage.output_tokens
                logger.info(f"API test call successful (used {input_tokens} input, {output_tokens} output tokens)")
                return True, None, "API access verified (balance not available via API)"
            except anthropic.AuthenticationError as e:
                return False, 0.0, f"Authentication failed: {e}"
            except anthropic.RateLimitError as e:
                error_msg = str(e).lower()
                if "credit" in error_msg or "balance" in error_msg or "insufficient" in error_msg:
                    return False, 0.0, f"Insufficient credits: {e}"
                # Rate limited but API key is valid
                return True, None, "API key valid (rate limited)"
            except anthropic.BadRequestError as e:
                error_msg = str(e).lower()
                if "credit" in error_msg or "balance" in error_msg:
                    return False, 0.0, f"Insufficient credits: {e}"
                return False, None, f"API error: {e}"

        except Exception as e:
            logger.error(f"Error checking API credits: {e}")
            return False, None, f"Error checking credits: {e}"

    def estimate_processing_cost(self, transcript: str) -> float:
        """
        Estimate the cost of processing a transcript.

        Args:
            transcript: The transcript text

        Returns:
            Estimated cost in USD
        """
        pricing = self.MODEL_PRICING.get(AI_model, {"input": 3.00, "output": 15.00})

        # Estimate input tokens
        transcript_tokens = self.estimate_tokens(transcript)
        static_tokens = self._static_prompt_tokens_max
        total_input_tokens = static_tokens + transcript_tokens

        # Estimate output tokens (conservative: assume max output)
        estimated_output_tokens = min(self.max_output_limit, transcript_tokens * 0.5)

        # Calculate costs (per 1M tokens)
        input_cost = (total_input_tokens / 1_000_000) * pricing["input"]
        output_cost = (estimated_output_tokens / 1_000_000) * pricing["output"]

        # If using dual scan mode, multiply by 2
        if self.scan_mode == "dual":
            input_cost *= 2
            output_cost *= 2

        return input_cost + output_cost

    def log_credit_status(self, balance: Optional[float], message: str) -> None:
        """Log credit status with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if balance is not None:
            logger.info(f"[{timestamp}] Credit Status: ${balance:.2f} USD - {message}")
        else:
            logger.info(f"[{timestamp}] Credit Status: {message}")

    async def check_credits_before_processing(self, filename: str, transcript: str) -> Tuple[bool, str]:
        """
        Check if there are sufficient credits before processing a file.

        Args:
            filename: Name of the file to be processed
            transcript: The transcript content

        Returns:
            Tuple of (can_proceed, message)
        """
        if not self.credits_check_enabled:
            return True, "Credit checking disabled"

        estimated_cost = self.estimate_processing_cost(transcript)
        logger.info(f"Estimated cost for {filename}: ${estimated_cost:.4f} USD")

        success, balance, message = self.get_api_credits()

        if not success:
            return False, message

        if balance is not None:
            if balance < self.min_credits_usd:
                return False, f"Insufficient credits: ${balance:.2f} USD (minimum: ${self.min_credits_usd:.2f} USD)"
            if balance < estimated_cost * 2:  # Warning threshold
                logger.warning(f"Low credit warning: ${balance:.2f} USD remaining")

        return True, message

    async def prompt_user_for_credits(self) -> bool:
        """
        Prompt user to confirm they have purchased more credits.

        Returns:
            True if user confirms continuation, False otherwise
        """
        print("\n" + "=" * 60)
        print("CREDITS INSUFFICIENT OR LOW")
        print("=" * 60)
        print(f"Minimum required: ${self.min_credits_usd:.2f} USD")
        print(f"Files processed so far: {self.files_processed}")
        print(f"Total cost this session: ${self.total_cost_session:.4f} USD")
        print("\nPlease purchase more credits at: https://console.anthropic.com/")
        print("=" * 60)

        while True:
            response = input("\nHave you purchased more credits? Continue processing? (yes/no): ").strip().lower()
            if response in ('yes', 'y', 'sim', 's'):
                logger.info("User confirmed credit purchase - continuing processing")
                return True
            elif response in ('no', 'n', 'nao', 'não'):
                logger.info("User chose to stop processing due to credits")
                return False
            else:
                print("Please answer 'yes' or 'no'")

    def _build_cached_system_prompt(self) -> str:
        """
        Build the system prompt optimized for Claude's prompt caching.
        The system prompt contains all static instructions and the variable dictionary.
        """
        return system_prompt

    def _resolve_scan_scopes(self, scan_mode: str) -> List[Optional[str]]:
        """Resolve scan scopes based on configured scan mode."""
        mode = (scan_mode or "single").lower().strip()
        if mode == "dual":
            return [self.SCAN_SCOPE_EXPLORATORIOS, self.SCAN_SCOPE_CLASSIFICATORIOS]
        if mode in ("exploratorios", self.SCAN_SCOPE_EXPLORATORIOS):
            return [self.SCAN_SCOPE_EXPLORATORIOS]
        if mode in ("classificatorios", self.SCAN_SCOPE_CLASSIFICATORIOS):
            return [self.SCAN_SCOPE_CLASSIFICATORIOS]
        return [None]

    def _normalize_yaml_content(self, yaml_content: str) -> str:
        """Normalize YAML content to improve parser compatibility."""
        # Remove inline comments
        yaml_content = re.sub(r'//.*$', '', yaml_content, flags=re.MULTILINE)
        yaml_content = re.sub(r'#.*$', '', yaml_content, flags=re.MULTILINE)

        lines = []
        for line in yaml_content.split('\n'):
            # Convert tabs to spaces
            line = line.replace('\t', '    ')

            # Fix nested quotes within YAML values
            if ': "' in line and line.count('"') > 2:
                if line.count(': "') == 1:
                    key_part, value_part = line.split(': "', 1)
                    if value_part.count('"') >= 2:
                        last_quote_idx = value_part.rfind('"')
                        value_before_close = value_part[:last_quote_idx]
                        value_close = value_part[last_quote_idx:]
                        value_before_close = value_before_close.replace('"', "'")
                        line = key_part + ': "' + value_before_close + value_close

            lines.append(line)
        return '\n'.join(lines)

    def _extract_yaml_block_text(self, variable_dict_text: str) -> Tuple[str, str, str]:
        """Extract prefix, YAML block content, and suffix from variable_dictionary."""
        yaml_matches = list(re.finditer(r'```yaml\s*(.*?)\s*```', variable_dict_text, re.DOTALL))
        if not yaml_matches:
            return "", "", ""

        data_match = yaml_matches[1] if len(yaml_matches) > 1 else yaml_matches[0]
        prefix = variable_dict_text[:data_match.start()]
        yaml_content = data_match.group(1)
        suffix = variable_dict_text[data_match.end():]
        return prefix, yaml_content, suffix

    def _parse_variable_dictionary_yaml(self, variable_dict_text: str) -> Dict[str, object]:
        """Parse the YAML dictionary block into a Python dict."""
        _, yaml_content, _ = self._extract_yaml_block_text(variable_dict_text)
        if not yaml_content:
            if not self._variable_dict_parse_failed:
                logger.warning("Could not extract YAML from variable_dictionary.")
            self._variable_dict_parse_failed = True
            return {}

        yaml_content = self._normalize_yaml_content(yaml_content)

        try:
            return yaml.safe_load(yaml_content) or {}
        except yaml.YAMLError as e:
            if not self._variable_dict_parse_failed:
                logger.warning(f"Failed to parse YAML from variable_dictionary: {e}")
            self._variable_dict_parse_failed = True
            return {}

    def _extract_yaml_scope_block(self, yaml_content: str, scope_root: str) -> str:
        """Extract a root scope from YAML text without parsing."""
        lines = yaml_content.splitlines()
        start = None
        end = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if line.lstrip() == line and stripped.startswith(f"{scope_root}:"):
                start = i
                continue
            if start is not None and line.lstrip() == line and stripped.endswith(":"):
                end = i
                break

        if start is None:
            return ""
        if end is None:
            end = len(lines)
        return "\n".join(lines[start:end]).strip()

    def _build_variable_dictionary_subset(self, scope_root: str) -> str:
        """Return variable_dictionary text limited to a single root scope."""
        if self._variable_dict_data and scope_root in self._variable_dict_data:
            prefix, _, suffix = self._extract_yaml_block_text(self._variable_dict_text)
            if not prefix and not suffix:
                return self._variable_dict_text
            filtered = {scope_root: self._variable_dict_data.get(scope_root, {})}
            filtered_yaml = yaml.safe_dump(filtered, allow_unicode=True, sort_keys=False).strip()
            new_block = f"```yaml\n{filtered_yaml}\n```"
            return prefix + new_block + suffix

        prefix, yaml_content, suffix = self._extract_yaml_block_text(self._variable_dict_text)
        if not yaml_content:
            return self._variable_dict_text

        scope_block = self._extract_yaml_scope_block(yaml_content, scope_root)
        if not scope_block:
            return self._variable_dict_text

        new_block = f"```yaml\n{scope_block}\n```"
        return prefix + new_block + suffix

    def _build_scan_scope_note(self, scope: Optional[str]) -> str:
        """Build scope-specific guardrails to keep the model focused."""
        if scope == self.SCAN_SCOPE_EXPLORATORIOS:
            return (
                "<scan_scope>\n"
                "MODO DE VARREDURA: fatores_exploratorios\n"
                "- Use SOMENTE variáveis de fatores_exploratorios\n"
                "- Ignore fatores_classificatorios\n"
                "</scan_scope>"
            )
        if scope == self.SCAN_SCOPE_CLASSIFICATORIOS:
            return (
                "<scan_scope>\n"
                "MODO DE VARREDURA: fatores_classificatorios\n"
                "- Use SOMENTE variáveis de fatores_classificatorios\n"
                "- Ignore fatores_exploratorios\n"
                "</scan_scope>"
            )
        return ""

    def _extract_keyword_mappings_from_yaml(self, scope: Optional[str] = None) -> Dict[str, List[str]]:
        """
        Dynamically extract keyword-to-variable mappings from the YAML variable dictionary.
        This ensures the keyword index stays synchronized with the dictionary automatically.

        Returns:
            Dictionary mapping keywords/phrases to variable names
        """
        if self._variable_dict_parse_failed:
            return {}

        yaml_data = self._variable_dict_data
        if not yaml_data:
            return {}

        keyword_map = {}

        def extract_keywords_from_definition(var_name: str, definition: str):
            """Extract meaningful keywords from a variable definition."""
            keywords = []

            # Common patterns to extract
            patterns = [
                r'"([^"]+)"',  # Text in quotes
                r'Ex[.:]\s*([^;.]+)',  # Examples after "Ex:", "Ex."
                r'e\.g\.\s*([^;.]+)',  # Examples after "e.g."
            ]

            for pattern in patterns:
                matches = re.findall(pattern, definition, re.IGNORECASE)
                for match in matches:
                    # Clean and split on common separators
                    terms = re.split(r'[,;]', match.strip())
                    for term in terms:
                        term = term.strip().lower()
                        if len(term) > 3 and len(term) < 50:  # Reasonable keyword length
                            keywords.append(term)

            # Also extract key terms from the main definition
            # Look for significant nouns and phrases
            key_terms = re.findall(r'\b([a-záàâãéèêíïóôõöúçñ]{4,}(?:\s+[a-záàâãéèêíïóôõöúçñ]{3,}){0,2})\b',
                                   definition.lower())

            # Filter common words
            stop_words = {'para', 'como', 'pela', 'pelo', 'seus', 'suas', 'essa', 'esse',
                         'esta', 'este', 'mais', 'pode', 'forma', 'sobre', 'após', 'desde'}

            for term in key_terms[:3]:  # Take first 3 significant terms
                if not any(stop in term for stop in stop_words):
                    keywords.append(term)

            return keywords[:5]  # Limit to 5 keywords per variable

        def traverse_dict(d, path=[]):
            """Recursively traverse the YAML structure to find leaf variables."""
            if isinstance(d, dict):
                for key, value in d.items():
                    if isinstance(value, str):
                        # This is a leaf node (variable definition)
                        var_name = key
                        keywords = extract_keywords_from_definition(var_name, value)

                        for keyword in keywords:
                            if keyword not in keyword_map:
                                keyword_map[keyword] = []
                            if var_name not in keyword_map[keyword]:
                                keyword_map[keyword].append(var_name)
                    elif isinstance(value, dict):
                        traverse_dict(value, path + [key])

        if scope == self.SCAN_SCOPE_EXPLORATORIOS and 'fatores_exploratorios' in yaml_data:
            traverse_dict(yaml_data['fatores_exploratorios'])
        elif scope == self.SCAN_SCOPE_CLASSIFICATORIOS and 'fatores_classificatorios' in yaml_data:
            traverse_dict(yaml_data['fatores_classificatorios'])
        else:
            if 'fatores_exploratorios' in yaml_data:
                traverse_dict(yaml_data['fatores_exploratorios'])
            if 'fatores_classificatorios' in yaml_data:
                traverse_dict(yaml_data['fatores_classificatorios'])

        logger.info(f"Extracted {len(keyword_map)} keyword mappings from YAML dictionary")
        return keyword_map

    def _build_exhaustive_extraction_instructions(self, scope: Optional[str] = None) -> str:
        """
        Build exhaustive extraction instructions that will be cached.
        These instructions guide the model to extract ALL variables systematically.
        Uses dynamic keyword mapping from YAML dictionary.
        """
        # Get dynamic keyword mappings
        keyword_map = self._extract_keyword_mappings_from_yaml(scope)

        # Build keyword index section dynamically
        keyword_index_lines = []
        for keyword, var_names in sorted(keyword_map.items())[:50]:  # Top 50 most relevant
            vars_str = ", ".join(var_names[:4])  # Max 4 variables per keyword
            keyword_index_lines.append(f'- "{keyword}" → {vars_str}')

        keyword_index = "\n".join(keyword_index_lines)

        category_lines = ""
        scope_guard = ""
        manual_keywords = ""
        if scope == self.SCAN_SCOPE_EXPLORATORIOS:
            category_lines = "   - Exploratórias: drivers_religiosos, dom, etica_crista, psico_emocionais, percepcao_impacto, valores_seculares, caracteristicas_adm"
            scope_guard = "   - IGNORE variáveis classificatórias."
        elif scope == self.SCAN_SCOPE_CLASSIFICATORIOS:
            category_lines = "   - Classificatórias: conversao, graca, aperfeicoamento, gratidao, modelo_gestao, sacrificio, esg/ods, rsc, esfera_soberania"
            scope_guard = "   - IGNORE variáveis exploratórias."
            manual_keywords = """
CATEGORIAS CRÍTICAS FREQUENTEMENTE SUBDETECTADAS:

→ ODS (OBJETIVOS DE DESENVOLVIMENTO SUSTENTÁVEL) - ATENÇÃO ESPECIAL:
  IMPORTANTE: ODS são frequentemente mencionados de forma INDIRETA ou com SINÔNIMOS.
  Busque manifestações PRÁTICAS, não apenas jargão técnico de sustentabilidade.

  ODS 1 - erradicacao_da_pobreza:
    Keywords: "pobreza", "pobre", "miserável", "carente", "necessitado", "vulnerável"

  ODS 2 - fome_zero_e_agricultura_sustentavel:
    Keywords: "fome", "alimentação", "agricultura", "alimento", "nutrição", "comida"

  ODS 3 - saude_e_bem_estar:
    Keywords: "saúde", "bem-estar", "doença", "hospital", "médico", "tratamento", "cuidar"

  ODS 4 - educacao_de_qualidade:
    Keywords: "educação", "ensino", "escola", "aprendizado", "formação", "capacitação"

  ODS 5 - igualdade_de_genero:
    Keywords: "gênero", "mulher", "feminino", "igualdade", "equidade de gênero"

  ODS 6 - agua_potavel_e_saneamento:
    Keywords: "água", "saneamento", "higiene", "esgoto", "água potável"

  ODS 7 - energia_limpa_e_acessivel:
    Keywords: "energia", "renovável", "elétrica", "energia limpa", "sustentável"

  ODS 8 - trabalho_decente_e_crescimento_economico:
    Keywords: "trabalho decente", "emprego", "crescimento", "economia", "renda", "trabalhador"

  ODS 9 - industria_inovacao_e_infraestrutura:
    Keywords: "indústria", "inovação", "infraestrutura", "tecnologia", "industrial"

  ODS 10 - reducao_das_desigualdades:
    Keywords: "desigualdade", "inclusão", "discriminação", "equidade", "justiça social"

  ODS 11 - cidades_e_comunidades_sustentaveis:
    Keywords: "cidade", "comunidade", "urbano", "habitação", "moradia", "bairro"

  ODS 12 - consumo_e_producao_responsaveis:
    Keywords: "consumo", "produção", "resíduo", "desperdício", "sustentável", "reciclagem"

  ODS 13 - acao_contra_mudanca_global_do_clima:
    Keywords: "clima", "mudança climática", "aquecimento", "emissão", "carbono", "CO2"

  ODS 14 - vida_na_agua:
    Keywords: "oceano", "mar", "pesca", "marinho", "vida aquática", "água do mar"

  ODS 15 - vida_terrestre:
    Keywords: "floresta", "biodiversidade", "desmatamento", "ecossistema", "fauna", "flora"

  ODS 16 - paz_justica_e_instituicoes_eficazes:
    Keywords: "paz", "justiça", "instituição", "corrupção", "transparência", "governança"

  ODS 17 - parcerias_e_meios_de_implementacao:
    Keywords: "parceria", "cooperação", "aliança", "colaboração", "rede", "global"

→ ESFERA DE SOBERANIA (ASPECTOS DA REALIDADE):
  - números, quantidades, "funcionários", "unidades" → aspecto_numerico_1
  - espaço, lugar, "localização", "região" → aspecto_espacial_2
  - movimento, "crescimento", "mudança ao longo do tempo" → aspecto_cinetico_3
  - físico, material, "equipamento", "estrutura" → aspecto_fisico_4
  - vida, saúde, "cuidar", "bem-estar físico" → aspecto_biotico_5
  - sentimento, emoção, "dor", "alegria", "medo" → aspecto_sensitivo_6
  - lógica, razão, "pensar", "analisar" → aspecto_analitico_7
  - formar, criar, "planejar", "desenvolver" → aspecto_formativo_8
  - comunicação, "falar", "conversar", "linguagem" → aspecto_linguistico_9
  - relações, "equipe", "convivência", "social" → aspecto_social_10
  - dinheiro, recursos, "lucro", "custo", "econômico" → aspecto_economico_11
  - beleza, estética, "design" → aspecto_estetico_12
  - lei, norma, "direito", "justiça legal" → aspecto_juridico_13
  - amor, moral, "integridade", "ética" → aspecto_etico_14
  - fé, "Deus", "oração", "transcendente" → aspecto_pistico_15

→ ESG E PERCEPÇÕES:
  - "ESG", "sustentabilidade corporativa", "governança" → todas as subcategorias de esg
  - "agenda", "ideologia", "progressismo" → esg_como_agenda_ideologica_e_de_poder
  - "obrigação", "burocracia", "compliance" → esg_como_aparato_normativo_e_anticompetitivo
  - "marketing", "greenwashing", "hipocrisia" → esg_como_capital_simbolico_e_reputacional
- "Jesus ordenou", "mandamento cristão" → obrigacoes_que_jesus_ordenou
"""
        else:
            category_lines = (
                "   - Classificatórias: conversao, graca, aperfeicoamento, gratidao, modelo_gestao, sacrificio, esg/ods, rsc, esfera_soberania\n"
                "   - Exploratórias: drivers_religiosos, dom, etica_crista, psico_emocionais, percepcao_impacto, valores_seculares, caracteristicas_adm"
            )

        return f"""
INSTRUÇÕES DE EXTRAÇÃO EXAUSTIVA:

1. SEGMENTE o transcript em unidades semânticas (1-3 frases por unidade)

2. PARA CADA UNIDADE, verifique TODAS as variáveis dentro do escopo:
{category_lines}
{scope_guard}

3. MULTI-CODIFICAÇÃO É OBRIGATÓRIA:
   - Cada ITEM deve ter 3-10 variáveis (média esperada: 5+)
   - Se encontrou 1 variável, busque 4+ mais no MESMO trecho

4. METAS DE COBERTURA (IMPORTANTE):
   - Mínimo: 80 ITEMs por entrevista típica
   - Média esperada: 100-120 ITEMs
   - Variáveis totais: 200-400 por entrevista

5. VIÉS PARA INCLUSÃO: Na dúvida, SEMPRE INCLUA com justificativa.

{manual_keywords}

ÍNDICE DINÂMICO DE PALAVRAS-CHAVE → VARIÁVEIS:
(Extraído automaticamente do dicionário YAML)

{keyword_index}

ATENÇÃO ESPECIAL - BUSQUE MANIFESTAÇÕES PRÁTICAS (não jargão teológico):
- Use palavras SIMPLES do dia-a-dia do entrevistado
- Não exija terminologia técnica
- Valorize expressões coloquiais e concretas
"""

    def _build_cached_user_prefix(self, scope: Optional[str] = None) -> str:
        """
        Build the static user message prefix that will be cached.
        This includes task, instructions, variable dictionary, output format, and exhaustive extraction instructions.
        The transcript will be appended dynamically.
        """
        variable_dictionary = self._variable_dictionary_by_scope.get(
            scope,
            self.static_prompt_parts["variable_dictionary"]
        )
        scope_note = self._build_scan_scope_note(scope)
        parts = [
            self.static_prompt_parts["task"],
            self.static_prompt_parts["instructions"]
        ]
        if scope_note:
            parts.append(scope_note)
        parts.extend([
            variable_dictionary,
            self.static_prompt_parts["output_format"],
            self._build_exhaustive_extraction_instructions(scope)
        ])
        return "\n\n".join(parts)

    def _build_base_prompt_template(self) -> str:
        """Build the static parts of the prompt once to avoid repeated string concatenation."""
        return "\n\n".join([
            self.static_prompt_parts["task"],
            "{transcript_part}",  # Placeholder for dynamic content
            self.static_prompt_parts["instructions"],
            self.static_prompt_parts["variable_dictionary"],
            self.static_prompt_parts["output_format"],
            "Analyze the interview transcript and provide your analysis in the exact format specified above."
        ])

    def _build_recall_block(self, scope: Optional[str]) -> str:
        """Build a scope-aware recall block to keep the model focused."""
        if scope == self.SCAN_SCOPE_EXPLORATORIOS:
            return (
                "MODO DE VARREDURA (EXPLORATORIOS):\n"
                "- Use SOMENTE variáveis de fatores_exploratorios\n"
                "- Ignore fatores_classificatorios\n"
                "- Se não houver evidência literal forte, NÃO gere ITEM\n\n"
                "AGORA COMECE A EXTRAÇÃO."
            )
        if scope == self.SCAN_SCOPE_CLASSIFICATORIOS:
            return (
                "MODO DE VARREDURA (CLASSIFICATORIOS):\n"
                "- Use SOMENTE variáveis de fatores_classificatorios\n"
                "- Ignore fatores_exploratorios\n"
                "- Se não houver evidência literal forte, NÃO gere ITEM\n\n"
                "AGORA COMECE A EXTRAÇÃO."
            )

        return """
═══════════════════════════════════════════════════════════
ANTES DE GERAR O OUTPUT, FAÇA UM RECALL COMPLETO:
═══════════════════════════════════════════════════════════

1. RELEIA mentalmente o transcript inteiro acima

2. RECALL ESPECÍFICO POR CATEGORIA:

   A) VARIÁVEIS CLASSIFICATÓRIAS:
      - conversao: Busque eventos de conhecer Jesus, batismo, transformação
      - graca: Busque "recebi", "Deus me deu", favor imerecido
      - gratidao: Busque "agradecer", "grato", reconhecimento
      - modelo_gestao: Busque menção a lucro, prejuízo, custos, resultados financeiros
      - sacrificio: Busque perdas, renúncias, prejuízos por fé
      - esg/ods: Busque sustentabilidade, social, ambiental, governança
      - rsc: Busque responsabilidade social, práticas éticas
      - esfera_soberania: Busque números, espaço, tempo, recursos, comunicação, emoções, fé

   B) ODS (BUSQUE ESPECIALMENTE - ALTA PRIORIDADE):
      IMPORTANTE: ODS são expressões PRÁTICAS, não jargão técnico.
      Busque manifestações CONCRETAS destes temas:

      → ODS 1 (erradicacao_da_pobreza): "pobre", "pobreza", "carente", "necessitado"
      → ODS 2 (fome_zero_e_agricultura_sustentavel): "fome", "alimentação", "agricultura"
      → ODS 3 (saude_e_bem_estar): "saúde", "bem-estar", "doença", "cuidar da saúde"
      → ODS 4 (educacao_de_qualidade): "educação", "ensino", "escola", "capacitação"
      → ODS 5 (igualdade_de_genero): "mulher", "gênero", "igualdade de gênero"
      → ODS 6 (agua_potavel_e_saneamento): "água", "saneamento", "higiene"
      → ODS 7 (energia_limpa_e_acessivel): "energia", "renovável", "energia limpa"
      → ODS 8 (trabalho_decente_e_crescimento_economico): "trabalho decente", "emprego", "trabalhador"
      → ODS 9 (industria_inovacao_e_infraestrutura): "indústria", "inovação", "tecnologia"
      → ODS 10 (reducao_das_desigualdades): "desigualdade", "inclusão", "discriminação"
      → ODS 11 (cidades_e_comunidades_sustentaveis): "cidade", "comunidade", "urbano"
      → ODS 12 (consumo_e_producao_responsaveis): "consumo", "produção", "desperdício"
      → ODS 13 (acao_contra_a_mudanca_global_do_clima): "clima", "emissão", "aquecimento"
      → ODS 14 (vida_na_agua): "mar", "oceano", "pesca", "marinho", "água do mar"
      → ODS 15 (vida_terrestre): "floresta", "biodiversidade", "desmatamento"
      → ODS 16 (paz_justica_e_instituicoes_eficazes): "paz", "justiça", "corrupção"
      → ODS 17 (parcerias_e_meios_de_implementacao): "parceria", "cooperação", "aliança"

   C) ESFERA DE SOBERANIA (BUSQUE ESPECIALMENTE):
      - Números/quantidades → aspecto_numerico_1
      - Espaço/lugar → aspecto_espacial_2
      - Movimento/mudança → aspecto_cinetico_3
      - Material/físico → aspecto_fisico_4
      - Vida/saúde → aspecto_biotico_5
      - Emoções/sentimentos → aspecto_sensitivo_6
      - Lógica/razão → aspecto_analitico_7
      - Planejar/formar → aspecto_formativo_8
      - Comunicação/linguagem → aspecto_linguistico_9
      - Relações/social → aspecto_social_10
      - Dinheiro/recursos → aspecto_economico_11
      - Beleza/estética → aspecto_estetico_12
      - Lei/norma → aspecto_juridico_13
      - Amor/moral → aspecto_etico_14
      - Fé/transcendente → aspecto_pistico_15

   D) DRIVERS PSICO-EMOCIONAIS:
      - medo, coragem, ansiedade, frustração, calma

3. Use o ÍNDICE DE PALAVRAS-CHAVE para identificar rapidamente variáveis

4. MULTI-CODIFIQUE: se encontrou 1 variável, busque 4+ mais no MESMO trecho

METAS DE COBERTURA (CRÍTICO):
- Mínimo: 80 ITEMs por entrevista típica
- Média esperada: 100-120 ITEMs
- Variáveis totais: 200-400 por entrevista

AGORA COMECE A EXTRAÇÃO. Gere TODOS os ITEMs com evidência literal."""

    def estimate_tokens(self, text: str) -> int:
        """Estimate tokens with caching to avoid repeated tokenization."""
        text_hash = hash(text)
        if text_hash in self._token_cache:
            return self._token_cache[text_hash]

        try:
            # Improved estimation for Portuguese: ~1.3 tokens per word (more conservative)
            # Portuguese tends to use more tokens due to accents and longer words
            words = len(text.split())
            tokens = int(words * 1.3)
            self._token_cache[text_hash] = tokens
            return tokens
        except Exception as e:
            logger.warning(f"Token estimation failed: {e}")
            fallback = len(text) // 3  # More conservative fallback
            self._token_cache[text_hash] = fallback
            return fallback

    def calculate_max_transcript_tokens(
        self,
        desired_output_tokens: int = None,
        scope: Optional[str] = None,
        static_tokens_override: Optional[int] = None
    ) -> int:
        """
        Calculate maximum tokens available for transcript content.

        Args:
            desired_output_tokens: Desired output tokens (defaults to model max)
            scope: Optional scan scope for prompt sizing
            static_tokens_override: Optional override for static token count

        Returns:
            Maximum tokens available for transcript
        """
        if desired_output_tokens is None:
            desired_output_tokens = self.max_output_limit
        if static_tokens_override is not None:
            static_tokens = static_tokens_override
        elif scope is not None:
            static_tokens = self._static_prompt_tokens_by_scope.get(scope, self._static_prompt_tokens)
        else:
            static_tokens = self._static_prompt_tokens

        # Reserve tokens: static prompt + output + safety margin (10%)
        safety_margin = int(self.model_token_limit * 0.10)
        available = self.model_token_limit - static_tokens - desired_output_tokens - safety_margin

        logger.debug(f"Token budget: {self.model_token_limit} total, "
                    f"{static_tokens} static, "
                    f"{desired_output_tokens} output, "
                    f"{safety_margin} margin = {available} for transcript")

        return max(available, 5000)  # Minimum 5000 tokens for transcript

    def create_cached_api_messages(
        self,
        interview_id: str,
        transcript: str,
        scope: Optional[str] = None
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Create API messages with prompt caching enabled.

        Returns:
            Tuple of (system_messages, user_messages) formatted for Claude API with cache_control
        """
        # Build dynamic transcript part with scope-aware recall prompt
        recall_block = self._build_recall_block(scope)
        transcript_block = f"""<interview_transcript>
interview_id: {interview_id}

{transcript}
</interview_transcript>

{recall_block}"""

        # System message with cache_control on static content
        system_messages = [
            {
                "type": "text",
                "text": self._cached_system_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]

        # User message: static prefix (cached) + dynamic transcript
        user_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": self._cached_user_prefix_by_scope.get(scope, self._cached_user_prefix),
                        "cache_control": {"type": "ephemeral"}
                    },
                    {
                        "type": "text",
                        "text": transcript_block
                    }
                ]
            }
        ]

        return system_messages, user_messages

    def validate_literal_match(self, ordem_1a: str, transcript: str, threshold: float = 0.95) -> tuple[bool, float]:
        """
        Validates if ordem_1a appears literally in the transcript.

        Args:
            ordem_1a: The extracted text that should be literal
            transcript: The original transcript to search in
            threshold: Minimum similarity score to accept (0.95 = 95%)

        Returns:
            tuple: (is_valid, similarity_score)
        """
        from difflib import SequenceMatcher

        # Normalize: remove extra whitespace but preserve content
        normalized_ordem = re.sub(r'\s+', ' ', ordem_1a.strip().lower())
        normalized_transcript = re.sub(r'\s+', ' ', transcript.strip().lower())

        # Test 1: Exact match (best case)
        if normalized_ordem in normalized_transcript:
            return True, 1.0

        # Test 2: Fuzzy matching with sliding window
        window_size = len(normalized_ordem)
        best_match = 0.0

        # Only check if window size is reasonable
        if window_size > len(normalized_transcript):
            logger.warning(f"ordem_1a longer than transcript: {len(normalized_ordem)} > {len(normalized_transcript)}")
            return False, 0.0

        # Slide window through transcript
        for i in range(len(normalized_transcript) - window_size + 1):
            window = normalized_transcript[i:i + window_size]
            ratio = SequenceMatcher(None, normalized_ordem, window).ratio()
            best_match = max(best_match, ratio)

            # Early exit if we found perfect match
            if best_match >= 0.999:
                break

        is_valid = best_match >= threshold
        return is_valid, best_match

    def text_similarity(self, text1: str, text2: str) -> float:
        """
        Calculate similarity between two texts using SequenceMatcher.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity score between 0.0 and 1.0
        """
        from difflib import SequenceMatcher

        # Normalize texts
        norm1 = re.sub(r'\s+', ' ', text1.strip().lower())
        norm2 = re.sub(r'\s+', ' ', text2.strip().lower())

        return SequenceMatcher(None, norm1, norm2).ratio()

    def truncate_transcript(self, transcript: str, max_tokens: int = None, scope: Optional[str] = None) -> str:
        """
        Truncates transcript to fit within token limit while preserving meaning.

        Args:
            transcript: The transcript text to truncate
            max_tokens: Maximum tokens allowed. If None, calculated dynamically based on model limits.

        Returns:
            Truncated transcript that fits within token limits
        """
        # Calculate dynamic max_tokens if not provided
        if max_tokens is None:
            max_tokens = self.calculate_max_transcript_tokens(scope=scope)

        estimated_tokens = self.estimate_tokens(transcript)
        if estimated_tokens <= max_tokens:
            return transcript

        logger.info(f"Truncating transcript: {estimated_tokens} tokens to {max_tokens}")
        sentences = nltk.sent_tokenize(transcript)
        truncated = ""
        current_tokens = 0

        for sentence in sentences:
            sentence_tokens = self.estimate_tokens(sentence)
            if current_tokens + sentence_tokens <= max_tokens:
                truncated += sentence + " "
                current_tokens += sentence_tokens
            else:
                break

        logger.info(f"Truncated transcript from {estimated_tokens} to {current_tokens} tokens")
        return truncated.strip()

    def create_prompt(self, interview_id: str, transcript: str) -> str:
        """Create prompt using cached template for better performance."""
        # Truncate transcript if necessary
        transcript = self.truncate_transcript(transcript)

        transcript_part = f"""<interview_transcript>
interview_id: {interview_id}

{transcript}
</interview_transcript>"""

        # First replace {interview_id} placeholders in the cached template
        prompt_with_id = self._cached_base_prompt.replace("{interview_id}", interview_id)

        # Then format with the transcript_part
        prompt = prompt_with_id.format(transcript_part=transcript_part)

        return prompt

    def should_throttle(self) -> bool:
        """Efficient rate limiting using deque."""
        now = time.time()
        while self.recent_calls and now - self.recent_calls[0] >= self.rate_window:
            self.recent_calls.popleft()
        return len(self.recent_calls) >= self.max_rpm

    def check_token_budget(self, input_tokens: int, max_output_tokens: int) -> bool:
        """Efficient token budget checking using deque."""
        now = time.time()

        while self.input_tokens_used and now - self.input_tokens_used[0][0] >= self.rate_window:
            self.input_tokens_used.popleft()

        while self.output_tokens_used and now - self.output_tokens_used[0][0] >= self.rate_window:
            self.output_tokens_used.popleft()

        current_input_usage = sum(tokens for _, tokens in self.input_tokens_used)
        current_output_usage = sum(tokens for _, tokens in self.output_tokens_used)

        return (current_input_usage + input_tokens <= self.max_input_tokens_per_minute and
                current_output_usage + max_output_tokens <= self.max_output_tokens_per_minute)

    def get_wait_time_for_rate_limit(self) -> float:
        """Calculate optimal wait time based on current rate limit status."""
        if not self.recent_calls:
            return 0.0

        now = time.time()
        oldest_call = self.recent_calls[0]
        time_until_oldest_expires = max(0, self.rate_window - (now - oldest_call))

        # Add small buffer
        return time_until_oldest_expires + 0.5

    def extract_header(self, response: str) -> str:
        """Extract header in .syn format: SOURCE @id ... END SOURCE"""
        # Try to find SOURCE block
        match = re.search(r'SOURCE @(.*?)END SOURCE', response, re.DOTALL)
        if match:
            return f"SOURCE @{match.group(1)}END SOURCE"
        # Try alternative format with delimiters
        match = re.search(r'\[-begin_header-\](.*?)\[-end_header-\]', response, re.DOTALL)
        if match:
            inner = match.group(1).strip()
            # Extract SOURCE block from inner content
            source_match = re.search(r'SOURCE @(.*?)END SOURCE', inner, re.DOTALL)
            if source_match:
                return f"SOURCE @{source_match.group(1)}END SOURCE"
        return ""

    def extract_items(self, response: str) -> List[str]:
        """Extract ITEM blocks in .syn format"""
        items = []

        # First try: ITEM @id ... END ITEM format (without delimiters)
        direct_items = re.findall(r'ITEM @(.*?)END ITEM', response, re.DOTALL | re.IGNORECASE)
        if direct_items:
            for item in direct_items:
                # Clean up the item content and ensure END ITEM is on its own line
                item_content = item.strip()
                # If END ITEM is not already on a new line, ensure it will be
                if not item_content.endswith('\n'):
                    items.append(f"ITEM @{item_content}\nEND ITEM")
                else:
                    items.append(f"ITEM @{item_content}END ITEM")

        # Second try: look for [-begin_item-] ... [-end_item-] format
        if not items:
            delimited_items = re.findall(r'\[-begin_item-\](.*?)\[-end_item-\]', response, re.DOTALL)
            for item_content in delimited_items:
                # Extract the ITEM block from within delimiters
                item_match = re.search(r'ITEM @(.*?)END ITEM', item_content, re.DOTALL | re.IGNORECASE)
                if item_match:
                    inner_content = item_match.group(1).strip()
                    items.append(f"ITEM @{inner_content}\nEND ITEM")
                else:
                    # Use the content as-is if no ITEM block found
                    items.append(item_content.strip())

        # Third try: look for lowercase 'item' and 'end item'
        if not items:
            lowercase_items = re.findall(r'item @(.*?)end item', response, re.DOTALL)
            for item in lowercase_items:
                item_content = item.strip()
                items.append(f"ITEM @{item_content}\nEND ITEM")

        return items

    def parse_item_fields(self, item_text: str) -> Dict[str, object]:
        """
        Parse an ITEM block into structured fields.

        Args:
            item_text: The complete ITEM text (e.g., "ITEM @id\n ordem_1a: ...\n END ITEM")

        Returns:
            Dictionary with keys: 'id', 'ordem_1a', 'pairs'
        """
        result = {
            'id': '',
            'ordem_1a': '',
            'pairs': []
        }

        # Extract ID
        id_match = re.search(r'ITEM @(\S+)', item_text, re.IGNORECASE)
        if id_match:
            result['id'] = id_match.group(1)

        # Extract ordem_1a
        ordem_1a_match = re.search(r'ordem_1a:\s*["\']?(.*?)["\']?\s*(?:ordem_2a:|$)', item_text, re.DOTALL | re.IGNORECASE)
        if ordem_1a_match:
            result['ordem_1a'] = ordem_1a_match.group(1).strip().strip('"').strip("'")

        # Extract all ordem_2a/justificativa_interna pairs
        pairs = []
        ordem_2a_lines = list(re.finditer(r'^\s*ordem_2a:\s*', item_text, re.IGNORECASE | re.MULTILINE))
        for idx, match in enumerate(ordem_2a_lines):
            start = match.start()
            end = ordem_2a_lines[idx + 1].start() if idx + 1 < len(ordem_2a_lines) else len(item_text)
            block = item_text[start:end]

            ordem_2a_match = re.search(
                r'^\s*ordem_2a:\s*["\']?(.*?)["\']?\s*(?:justificativa_interna:|END ITEM|$)',
                block,
                re.DOTALL | re.IGNORECASE | re.MULTILINE
            )
            justif_match = re.search(
                r'^\s*justificativa_interna:\s*["\']?(.*?)["\']?\s*(?:END ITEM|$)',
                block,
                re.DOTALL | re.IGNORECASE | re.MULTILINE
            )

            ordem_2a_value = ""
            justif_value = ""
            if ordem_2a_match:
                ordem_2a_value = ordem_2a_match.group(1).strip().strip('"').strip("'")
            if justif_match:
                justif_value = justif_match.group(1).strip().strip('"').strip("'")

            pairs.append({
                'ordem_2a': ordem_2a_value,
                'justificativa_interna': justif_value
            })

        result['pairs'] = pairs

        return result




    def _normalize_ordem_1a(self, text: str) -> str:
        return re.sub(r'\s+', ' ', text.strip().lower())

    def _extract_valid_variables_from_dictionary(self) -> set:
        """
        Extract all valid variable names from the variable dictionary.
        Returns a set of lowercase variable names for validation.
        """
        if hasattr(self, '_valid_variables_cache'):
            return self._valid_variables_cache

        variable_dict = self.static_prompt_parts.get("variable_dictionary", "")

        # Extract variable names from YAML-like structure
        # Variables are the keys at the deepest level (before the colon and definition)
        valid_vars = set()

        # Pattern to match leaf variable definitions: any indentation (spaces/tabs),
        # key followed by colon and a quoted description.
        # e.g., "\t\tdons_do_espirito: \"Capacitacoes...\""
        pattern = r"^[ \t]*([A-Za-z0-9_]+):\s*[\"']"
        matches = re.findall(pattern, variable_dict, re.MULTILINE)
        for match in matches:
            valid_vars.add(match.lower())

        # Also extract from the schema comments for commonly used patterns
        # e.g., graca_especial_ge, micro_csr, etc.
        additional_pattern = r"'([a-z_]+)'"
        additional_matches = re.findall(additional_pattern, variable_dict.lower())
        for match in additional_matches:
            if len(match) > 3:  # Filter out very short matches
                valid_vars.add(match)

        self._valid_variables_cache = valid_vars
        logger.info(f"Extracted {len(valid_vars)} valid variables from dictionary")
        return valid_vars

    def validate_variable_name(self, variable_name: str) -> Tuple[bool, str]:
        """
        Validate that a variable name exists in the dictionary.

        Returns:
            Tuple of (is_valid, corrected_name or original)
        """
        valid_vars = self._extract_valid_variables_from_dictionary()
        normalized = variable_name.lower().strip().strip("'\"")

        if normalized in valid_vars:
            return True, normalized

        # Try to find closest match
        from difflib import get_close_matches
        matches = get_close_matches(normalized, valid_vars, n=1, cutoff=0.8)
        if matches:
            logger.warning(f"Variable '{variable_name}' corrected to '{matches[0]}'")
            return True, matches[0]

        return False, normalized

    def log_extraction_statistics(self, items: List[str], interview_id: str) -> None:
        """
        Log statistics about extracted variables for monitoring coverage.
        """
        if not items:
            logger.info(f"[{interview_id}] No items extracted")
            return

        # Count variables by category
        variable_counts = {}
        total_pairs = 0

        for item in items:
            parsed = self.parse_item_fields(item)
            for pair in parsed['pairs']:
                var_name = pair['ordem_2a'].lower()
                variable_counts[var_name] = variable_counts.get(var_name, 0) + 1
                total_pairs += 1

        # Log summary
        logger.info(f"[{interview_id}] Extraction summary: {len(items)} ITEMs, {total_pairs} variable pairs")
        logger.info(f"[{interview_id}] Unique variables: {len(variable_counts)}")

        # Log top variables
        sorted_vars = sorted(variable_counts.items(), key=lambda x: -x[1])[:10]
        if sorted_vars:
            logger.info(f"[{interview_id}] Top variables: {sorted_vars}")

        # Check for key classificatory variables
        classificatory_vars = [
            'graca_especial_ge', 'graca_comum_gc', 'graca_secular_gs',
            'hereditariedade_cultural', 'evento_cristo', 'cristianismo_secular',
            'superabundancia', 'singularidade_monergismo', 'prioridade_monergismo',
            'incongruencia_monergismo', 'eficacia', 'nao_circularidade_monergismo',
            'gratidao_especial_ge', 'gratidao_comum_gc', 'gratidao_comum_negativa_gcn',
            'micro_csr', 'macro_csr'
        ]

        found_classificatory = [v for v in classificatory_vars if v in variable_counts]
        missing_classificatory = [v for v in classificatory_vars if v not in variable_counts]

        if found_classificatory:
            logger.info(f"[{interview_id}] Found classificatory: {found_classificatory}")
        if missing_classificatory:
            logger.debug(f"[{interview_id}] Missing classificatory (may be expected): {missing_classificatory[:5]}...")

    def _build_item_text(self, item_id: str, ordem_1a: str, pairs: List[Dict[str, str]]) -> str:
        pairs_lines = []
        for idx, pair in enumerate(pairs):
            if idx > 0:
                pairs_lines.append("")
            pairs_lines.append(f'    ordem_2a: {pair["ordem_2a"]}')
            pairs_lines.append(f'    justificativa_interna: "{pair["justificativa_interna"]}"')

        pairs_block = "\n".join(pairs_lines)
        return (
            f"ITEM @{item_id}\n"
            f'    ordem_1a: "{ordem_1a}"\n'
            f"{pairs_block}\n"
            f"END ITEM"
        )

    def deduplicate_items(self, items: List[str], similarity_threshold: float = 0.85) -> List[str]:
        """
        Merge ITEMs with the same ordem_1a to avoid dropping valid pairs.

        Args:
            items: List of ITEM text blocks
            similarity_threshold: Unused (kept for backward compatibility)

        Returns:
            List of merged ITEMs
        """
        if not items:
            return items

        merged_items = {}
        ordered_entries = []

        for item in items:
            parsed = self.parse_item_fields(item)
            ordem_1a = parsed['ordem_1a']

            if not ordem_1a:
                # Keep items without ordem_1a as-is (shouldn't happen but be safe)
                ordered_entries.append(("raw", item))
                continue

            key = self._normalize_ordem_1a(ordem_1a)
            if key not in merged_items:
                merged_items[key] = parsed
                ordered_entries.append(("key", key))
                continue

            existing = merged_items[key]
            seen = {(p['ordem_2a'], p['justificativa_interna']) for p in existing['pairs']}
            for pair in parsed['pairs']:
                signature = (pair['ordem_2a'], pair['justificativa_interna'])
                if signature not in seen:
                    existing['pairs'].append(pair)
                    seen.add(signature)

        merged_list = []
        for kind, value in ordered_entries:
            if kind == "raw":
                merged_list.append(value)
                continue
            parsed = merged_items[value]
            merged_list.append(
                self._build_item_text(parsed['id'], parsed['ordem_1a'], parsed['pairs'])
            )

        logger.info(f"Deduplication: {len(items)} items -> {len(merged_list)} merged items")
        return merged_list

    def split_large_transcript(
        self,
        transcript: str,
        max_chars: int = None,
        overlap_chars: int = 5000,
        static_tokens_override: Optional[int] = None
    ) -> List[str]:
        """
        Splits a large transcript into smaller chunks with overlap to maintain context.
        Uses dynamic token calculation to determine optimal chunk sizes.

        Args:
            transcript: The full transcript to split
            max_chars: Maximum characters per chunk. If None, calculated dynamically.
            overlap_chars: Number of characters to overlap between chunks (default 5000, increased for better context preservation)

        Returns:
            List of transcript chunks with contextual overlap
        """
        # Calculate max_chars dynamically based on available token budget
        if max_chars is None:
            # CRITICAL FIX: Use smaller chunks to avoid output truncation at 8000 tokens
            # Target: 18-25 ITEMs per chunk with ~3-5 variables each (≈4500-6500 output tokens)
            # Solution: Limit input to 6000 tokens ≈ 18000 chars per chunk
            # This ensures the model can generate complete output without hitting the 8000 token limit
            # Trade-off: More chunks = higher cost, but COMPLETE extraction without truncation
            max_tokens = min(
                self.calculate_max_transcript_tokens(static_tokens_override=static_tokens_override),
                6000
            )
            # Convert tokens to approximate characters (conservative: ~3 chars per token for Portuguese)
            max_chars = int(max_tokens * 3)
            logger.info(f"Dynamic chunk size: {max_tokens} tokens ≈ {max_chars} chars (optimized to avoid truncation)")

        if len(transcript) <= max_chars:
            return [transcript]

        logger.info(f"Splitting transcript ({len(transcript)} chars) into chunks of max {max_chars} chars with {overlap_chars} char overlap")
        chunks = []
        start = 0

        while start < len(transcript):
            end = start + max_chars

            # Don't exceed transcript length
            if end >= len(transcript):
                chunks.append(transcript[start:])
                break

            # Find the last sentence boundary before max_chars
            search_region = transcript[start:end]
            last_sentence = max(
                search_region.rfind('. '),
                search_region.rfind('! '),
                search_region.rfind('? '),
                search_region.rfind('.\n'),
                search_region.rfind('!\n'),
                search_region.rfind('?\n')
            )

            if last_sentence > 0:
                # Cut at sentence boundary
                end = start + last_sentence + 2  # +2 to include the punctuation and space
            else:
                # No sentence boundary found, cut at word boundary
                last_space = search_region.rfind(' ')
                if last_space > 0:
                    end = start + last_space

            chunk = transcript[start:end]
            chunks.append(chunk)

            # Next chunk starts with overlap
            start = end - overlap_chars

            # Ensure we make progress (safety check)
            if start < 0 or (len(chunks) > 1 and start <= len(transcript) - len(chunks[-1])):
                start = end

        logger.info(f"Created {len(chunks)} chunks with contextual overlap")
        return chunks

    async def _process_single_chunk(
        self,
        interview_id: str,
        transcript: str,
        scope: Optional[str] = None
    ) -> Tuple[str, str]:
        """Process a single transcript chunk with retries, rate limiting, and prompt caching."""
        # Throttle if nearing rate limit with smart wait time
        while self.should_throttle():
            wait_time = self.get_wait_time_for_rate_limit()
            logger.warning(f"Rate limit approaching for {interview_id}, waiting {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)

        # Truncate transcript to fit within token limits
        transcript = self.truncate_transcript(transcript, scope=scope)

        # Create cached API messages
        system_messages, user_messages = self.create_cached_api_messages(interview_id, transcript, scope=scope)

        # Estimate tokens for rate limiting
        transcript_tokens = self.estimate_tokens(transcript)
        static_tokens = self._static_prompt_tokens_by_scope.get(scope, self._static_prompt_tokens)
        input_tokens = static_tokens + transcript_tokens

        # Calculate output tokens - use model max but cap at rate limit
        max_output_tokens = min(self.max_output_limit, self.max_output_tokens_per_minute)

        # Verify total tokens don't exceed model limit
        total_estimated = input_tokens + max_output_tokens
        if total_estimated > self.model_token_limit:
            # Reduce output tokens to fit
            max_output_tokens = max(2000, self.model_token_limit - input_tokens - 1000)
            logger.warning(f"Reduced output tokens to {max_output_tokens} to fit model limit")

        # Wait if token budget is exceeded
        while not self.check_token_budget(input_tokens, max_output_tokens):
            logger.warning(f"Token budget exceeded for {interview_id}, waiting...")
            await asyncio.sleep(6)

        # Track this API call
        now = time.time()
        self.recent_calls.append(now)
        self.input_tokens_used.append((now, input_tokens))
        self.output_tokens_used.append((now, max_output_tokens))

        # Make the API call with prompt caching enabled
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.messages.create,
                    model=AI_model,
                    max_tokens=max_output_tokens,
                    temperature=0.0,
                    system=system_messages,
                    messages=user_messages,
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
                ),
                timeout=600
            )
            content = response.content[0].text

            # Log cache performance
            usage = response.usage
            cache_creation = getattr(usage, 'cache_creation_input_tokens', 0)
            cache_read = getattr(usage, 'cache_read_input_tokens', 0)
            if cache_creation or cache_read:
                logger.info(f"Cache stats for {interview_id}: "
                           f"created={cache_creation}, read={cache_read}, "
                           f"input={usage.input_tokens}, output={usage.output_tokens}")
            else:
                logger.info(f"Token usage for {interview_id}: "
                           f"input={usage.input_tokens}, output={usage.output_tokens}")

        except asyncio.TimeoutError:
            logger.error(f"API call timed out for {interview_id} after 600 seconds")
            raise anthropic.APITimeoutError("Request timed out after 10 minutes")

        # Validate and fix response format
        validated_content = self.validate_response_format(interview_id, content)
        logger.info(f"Successfully processed {interview_id}")

        return interview_id, validated_content

    def combine_chunk_outputs(self, original_id: str, chunk_outputs: List[str], original_transcript: str = "") -> str:
        """
        Combines outputs from multiple chunks with deduplication and validation.

        Args:
            original_id: The original interview ID
            chunk_outputs: List of outputs from each chunk
            original_transcript: The full original transcript for validation

        Returns:
            Combined output with deduplicated items
        """
        if not chunk_outputs:
            return f"SOURCE @{original_id}\n    código: N/A\nEND SOURCE"

        # Extract header from first chunk as base
        combined_header = self.extract_header(chunk_outputs[0])
        if not combined_header:
            combined_header = f"SOURCE @{original_id}\n    código: N/A\nEND SOURCE"

        # Replace any chunk reference IDs with the original
        combined_header = re.sub(r'@.*?_part\d+', f'@{original_id}', combined_header)

        # Collect all items from all chunks
        all_items = []
        for output in chunk_outputs:
            items = self.extract_items(output)
            for item in items:
                # Replace any chunk reference IDs with the original
                item = re.sub(r'@.*?_part\d+', f'@{original_id}', item)
                all_items.append(item)

        logger.info(f"Collected {len(all_items)} items from {len(chunk_outputs)} chunks before deduplication")

        # Deduplicate semantically similar items
        unique_items = self.deduplicate_items(all_items, similarity_threshold=0.85)

        # If original transcript provided, validate literalness
        if original_transcript:
            validated_items = []
            for item in unique_items:
                parsed = self.parse_item_fields(item)
                ordem_1a = parsed['ordem_1a']

                if ordem_1a:
                    is_valid, score = self.validate_literal_match(ordem_1a, original_transcript, threshold=0.95)
                    if is_valid:
                        validated_items.append(item)
                    else:
                        logger.warning(f"Item rejected (literalness={score:.2f}): '{ordem_1a[:50]}...'")
                else:
                    # Keep items without ordem_1a (shouldn't happen)
                    validated_items.append(item)

            logger.info(f"Validation: {len(unique_items)} items → {len(validated_items)} validated items")
            unique_items = validated_items

        # Log extraction statistics
        self.log_extraction_statistics(unique_items, original_id)

        # Combine everything
        if unique_items:
            items_text = "\n\n".join(unique_items)
            combined_output = combined_header + "\n\n" + items_text
        else:
            combined_output = combined_header

        return combined_output

    def _flush_output_buffer(self) -> None:
        """Write buffered results to file."""
        if self.output_buffer:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write("\n\n".join(self.output_buffer) + "\n\n")
            logger.info(f"Flushed {len(self.output_buffer)} results to file")
            self.output_buffer.clear()

    def validate_response_format(self, interview_id: str, content: str) -> str:
        """
        Validate and format response with strict quality checks.

        This method now enforces:
        - Presence of justificativa_interna in all ITEMs
        - Proper field structure
        - Valid ITEM format
        """

        # Check for valid header
        header = self.extract_header(content)
        if not header:
            logger.warning(f"Missing header for {interview_id}, creating default")
            header = f"SOURCE @{interview_id}\n    código: N/A\nEND SOURCE"

        # Extract items
        items = self.extract_items(content)

        if not items:
            logger.warning(f"No items found for {interview_id}")
            # Return just the header - silêncio estratégico is acceptable
            return header

        # Validate and format each item
        validated_items = []
        rejected_count = 0

        for item in items:
            item_stripped = item.strip()

            # Parse fields
            parsed = self.parse_item_fields(item_stripped)

            # Validation checks
            is_valid = True
            reasons = []

            # Check 1: Must have ordem_1a
            if not parsed['ordem_1a']:
                is_valid = False
                reasons.append("missing ordem_1a")

            # Check 2: Keep only complete ordem_2a/justificativa_interna pairs
            valid_pairs = []
            if not parsed['pairs']:
                reasons.append("missing ordem_2a")
                reasons.append("missing justificativa_interna")

            for pair_index, pair in enumerate(parsed['pairs'], start=1):
                if not pair['ordem_2a']:
                    reasons.append(f"missing ordem_2a[{pair_index}]")
                    continue

                # Relaxed validation: Accept partial justifications and auto-generate minimal ones
                if not pair['justificativa_interna']:
                    # Generate minimal justification automatically
                    var_name = pair['ordem_2a']
                    pair['justificativa_interna'] = f"Evidência textual de {var_name} encontrada no trecho literal."
                    logger.info(f"[{interview_id}] Generated minimal justification for {var_name}")
                    reasons.append(f"auto-generated justificativa_interna[{pair_index}]")

                # Normalize variable name
                var_name = pair['ordem_2a']
                if ' ' in var_name:
                    var_name = var_name.replace(' ', '_').lower()
                    logger.warning(f"ordem_2a had spaces, normalized to: '{var_name}'")

                # Validate against dictionary
                is_valid_var, corrected_name = self.validate_variable_name(var_name)
                if not is_valid_var:
                    logger.warning(f"Unknown variable '{var_name}', keeping as-is")
                else:
                    var_name = corrected_name

                pair['ordem_2a'] = var_name
                valid_pairs.append(pair)

            if not valid_pairs:
                is_valid = False
                if parsed['pairs']:
                    reasons.append("missing complete ordem_2a/justificativa_interna pairs")
            else:
                parsed['pairs'] = valid_pairs

            if not is_valid:
                logger.warning(f"ITEM rejected for {interview_id}: {', '.join(reasons)}")
                logger.warning(f"  ordem_1a: '{parsed['ordem_1a'][:50]}...'")
                rejected_count += 1
                continue

            # Reconstruct item with validated fields
            pairs_lines = []
            for idx, pair in enumerate(parsed['pairs']):
                if idx > 0:
                    pairs_lines.append("")
                pairs_lines.append(f"    ordem_2a: {pair['ordem_2a']}")
                pairs_lines.append(f"    justificativa_interna: \"{pair['justificativa_interna']}\"")

            pairs_block = "\n".join(pairs_lines)
            validated_item = f"""ITEM @{interview_id}
    ordem_1a: "{parsed['ordem_1a']}"
{pairs_block}
END ITEM"""

            validated_items.append(validated_item)

        if rejected_count > 0:
            logger.warning(f"Rejected {rejected_count}/{len(items)} items due to validation failures")

        # Combine output
        if validated_items:
            output = header + "\n\n" + "\n\n".join(validated_items)
        else:
            # All items rejected - return just header (silêncio estratégico)
            logger.warning(f"All items rejected for {interview_id}, returning empty result")
            output = header

        return output

    @retry(
        wait=wait_exponential(multiplier=2, min=8, max=180),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError
        ))
    )
    async def process_transcript(self, interview_id: str, transcript: str) -> Tuple[str, str]:
        """Processes a single interview transcript asynchronously with retries and rate limiting."""
        async with self.semaphore:
            logger.info(f"Processing transcript for {interview_id}")

            # Split large transcripts into manageable chunks
            transcript_chunks = self.split_large_transcript(
                transcript,
                static_tokens_override=self._static_prompt_tokens_max
            )
            chunked = len(transcript_chunks) > 1
            if chunked:
                logger.info(f"Split large transcript {interview_id} into {len(transcript_chunks)} chunks")

            all_outputs = []
            for i, chunk in enumerate(transcript_chunks):
                chunk_id = f"{interview_id}_part{i+1}" if chunked else interview_id
                for scope in self.scan_scopes:
                    scope_label = scope or "full"
                    try:
                        if chunked:
                            logger.info(
                                f"Processing chunk {i+1}/{len(transcript_chunks)} for {interview_id} "
                                f"[{scope_label}] ({len(chunk)} chars)"
                            )
                        _, chunk_output = await self._process_single_chunk(chunk_id, chunk, scope=scope)
                        all_outputs.append(chunk_output)
                        if chunked:
                            logger.info(
                                f"Successfully processed chunk {i+1}/{len(transcript_chunks)} "
                                f"for {interview_id} [{scope_label}]"
                            )
                    except Exception as e:
                        logger.error(
                            f"Failed to process chunk {i+1} of {interview_id} [{scope_label}]: {e}"
                        )
                        # Continue processing remaining chunks instead of failing completely

            if len(all_outputs) == 1:
                return interview_id, all_outputs[0]

            # Combine the outputs into a single result with validation
            combined_output = self.combine_chunk_outputs(interview_id, all_outputs, original_transcript=transcript)
            return interview_id, combined_output

    async def process_single_file(self, input_file: str) -> None:
        """Process a single text file containing an interview transcript."""

        # Read the input file
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                transcript = f.read()
        except Exception as e:
            logger.error(f"Failed to read input file {input_file}: {e}")
            raise

        # Extract interview_id from filename (without extension)
        interview_id = os.path.splitext(os.path.basename(input_file))[0]

        logger.info(f"Processing interview from {input_file}")
        logger.info(f"Interview ID: {interview_id}")
        logger.info(f"Transcript length: {len(transcript)} characters")

        # Process the transcript
        try:
            _, output = await self.process_transcript(interview_id, transcript)

            # Write to output file
            with open(self.output_file, "w", encoding="utf-8") as f:
                f.write(output)

            logger.info(f"Successfully processed interview {interview_id}")
            logger.info(f"Output saved to {self.output_file}")

        except Exception as e:
            logger.error(f"Failed to process interview {interview_id}: {e}")
            raise

    async def process_folder(self, input_folder: str, output_folder: str = None) -> Dict[str, Any]:
        """
        Process all .txt files in a folder sequentially.

        Args:
            input_folder: Folder containing .txt files
            output_folder: Optional output folder (defaults to same as input)

        Returns:
            Dictionary with processing statistics
        """
        start_time = datetime.now()
        logger.info("=" * 60)
        logger.info("BATCH PROCESSING STARTED")
        logger.info(f"Input folder: {input_folder}")
        logger.info(f"Start time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # Check initial API credits
        if self.credits_check_enabled:
            success, balance, message = self.get_api_credits()
            self.initial_credits = balance
            self.log_credit_status(balance, message)
            if balance is not None:
                logger.info(f"INITIAL CREDITS: ${balance:.2f} USD")
                print(f"\n*** INITIAL API CREDITS: ${balance:.2f} USD ***\n")
            else:
                logger.info(f"INITIAL CREDITS: {message}")
                print(f"\n*** API STATUS: {message} ***\n")

            if not success:
                logger.error(f"Cannot proceed: {message}")
                print(f"\nERROR: {message}")
                return {
                    "success": False,
                    "error": message,
                    "files_processed": 0,
                    "files_failed": 0
                }

        # Validate input folder
        if not os.path.isdir(input_folder):
            error_msg = f"Input folder not found: {input_folder}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "files_processed": 0, "files_failed": 0}

        # Get list of .txt files
        txt_files = sorted(glob(os.path.join(input_folder, "*.txt")))

        if not txt_files:
            error_msg = f"No .txt files found in: {input_folder}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "files_processed": 0, "files_failed": 0}

        logger.info(f"Found {len(txt_files)} .txt files to process")
        print(f"Found {len(txt_files)} interview files to process")

        # Set output folder
        if output_folder is None:
            output_folder = input_folder

        os.makedirs(output_folder, exist_ok=True)

        # Statistics
        results = {
            "success": True,
            "files_total": len(txt_files),
            "files_processed": 0,
            "files_failed": 0,
            "files_skipped": 0,
            "total_cost": 0.0,
            "details": []
        }

        # Process each file sequentially
        for idx, input_file in enumerate(txt_files, 1):
            filename = os.path.basename(input_file)
            interview_id = os.path.splitext(filename)[0]
            output_file = os.path.join(output_folder, f"{interview_id}.syn")

            print(f"\n[{idx}/{len(txt_files)}] Processing: {filename}")
            logger.info(f"[{idx}/{len(txt_files)}] Starting: {filename}")

            try:
                # Read transcript
                with open(input_file, 'r', encoding='utf-8') as f:
                    transcript = f.read()

                # Check credits before processing
                if self.credits_check_enabled:
                    can_proceed, credit_msg = await self.check_credits_before_processing(filename, transcript)

                    if not can_proceed:
                        logger.warning(f"Credit check failed for {filename}: {credit_msg}")
                        print(f"\n*** CREDITS LOW: {credit_msg} ***")

                        # Prompt user to continue
                        if not await self.prompt_user_for_credits():
                            logger.info("Batch processing stopped by user due to credits")
                            results["stopped_reason"] = "insufficient_credits"
                            break

                        # Re-check after user confirms
                        can_proceed, credit_msg = await self.check_credits_before_processing(filename, transcript)
                        if not can_proceed:
                            logger.error(f"Still cannot proceed: {credit_msg}")
                            results["files_skipped"] += 1
                            continue

                # Estimate cost
                estimated_cost = self.estimate_processing_cost(transcript)
                logger.info(f"Estimated cost for {filename}: ${estimated_cost:.4f} USD")

                # Process the transcript
                file_start = datetime.now()
                logger.info(f"Processing {filename} ({len(transcript)} chars)")

                # Temporarily change output file
                original_output = self.output_file
                self.output_file = output_file

                try:
                    _, output = await self.process_transcript(interview_id, transcript)

                    # Write output
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(output)

                    file_duration = (datetime.now() - file_start).total_seconds()
                    self.files_processed += 1
                    results["files_processed"] += 1
                    self.total_cost_session += estimated_cost
                    results["total_cost"] += estimated_cost

                    logger.info(f"SUCCESS: {filename} -> {os.path.basename(output_file)} ({file_duration:.1f}s)")
                    print(f"    SUCCESS: Output saved to {os.path.basename(output_file)}")

                    results["details"].append({
                        "file": filename,
                        "status": "success",
                        "output": output_file,
                        "duration": file_duration,
                        "estimated_cost": estimated_cost
                    })

                finally:
                    self.output_file = original_output

            except Exception as e:
                self.files_failed += 1
                results["files_failed"] += 1
                logger.error(f"FAILED: {filename} - {e}")
                print(f"    FAILED: {e}")

                results["details"].append({
                    "file": filename,
                    "status": "failed",
                    "error": str(e)
                })

        # Final statistics
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.info("=" * 60)
        logger.info("BATCH PROCESSING COMPLETED")
        logger.info(f"End time: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Total duration: {duration:.1f} seconds ({duration/60:.1f} minutes)")
        logger.info(f"Files processed: {results['files_processed']}/{results['files_total']}")
        logger.info(f"Files failed: {results['files_failed']}")
        logger.info(f"Files skipped: {results['files_skipped']}")
        logger.info(f"Total estimated cost: ${results['total_cost']:.4f} USD")

        # Final credit check
        if self.credits_check_enabled:
            success, final_balance, message = self.get_api_credits()
            if final_balance is not None:
                logger.info(f"FINAL CREDITS: ${final_balance:.2f} USD")
                if self.initial_credits is not None:
                    credits_used = self.initial_credits - final_balance
                    logger.info(f"ACTUAL CREDITS USED: ${credits_used:.2f} USD")
            else:
                logger.info(f"FINAL CREDITS: {message}")

        logger.info("=" * 60)

        # Print summary
        print("\n" + "=" * 60)
        print("BATCH PROCESSING SUMMARY")
        print("=" * 60)
        print(f"Total files: {results['files_total']}")
        print(f"Processed: {results['files_processed']}")
        print(f"Failed: {results['files_failed']}")
        print(f"Skipped: {results['files_skipped']}")
        print(f"Duration: {duration/60:.1f} minutes")
        print(f"Estimated cost: ${results['total_cost']:.4f} USD")
        print("=" * 60)

        return results


async def main():
    parser = argparse.ArgumentParser(
        description="Process interview transcripts using Claude API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a single file
  python interview_processor.py interview1.txt

  # Process all files in a folder (batch mode)
  python interview_processor.py --folder interviews/

  # Batch processing with custom settings
  python interview_processor.py --folder interviews/ --min-credits 2.0 --scan-mode dual
        """
    )

    # Input options (mutually exclusive: single file or folder)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "input_file",
        nargs="?",
        default=None,
        help="Input .txt file containing the interview transcript (single file mode)"
    )
    input_group.add_argument(
        "--folder", "-f",
        dest="input_folder",
        help="Input folder containing .txt files (batch mode)"
    )

    # API settings
    parser.add_argument(
        "--api-key",
        default=DEFAULT_API_KEY,
        help="Claude API key"
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Max concurrent requests"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Max retries per request"
    )
    parser.add_argument(
        "--scan-mode",
        default=DEFAULT_SCAN_MODE,
        choices=["single", "dual", "exploratorios", "classificatorios"],
        help="Scan mode: single (full), dual (exploratorios + classificatorios), or scoped"
    )

    # Credit control settings
    parser.add_argument(
        "--min-credits",
        type=float,
        default=DEFAULT_MIN_CREDITS_USD,
        help=f"Minimum credits (USD) required to continue processing (default: ${DEFAULT_MIN_CREDITS_USD:.2f})"
    )
    parser.add_argument(
        "--no-credit-check",
        action="store_true",
        help="Disable credit checking before each file"
    )

    # Output settings
    parser.add_argument(
        "--output-folder", "-o",
        help="Output folder for batch processing (default: same as input folder)"
    )

    args = parser.parse_args()

    # Determine processing mode
    batch_mode = args.input_folder is not None

    if batch_mode:
        # BATCH MODE: Process folder
        input_folder = args.input_folder
        output_folder = args.output_folder or input_folder

        # Validate input folder
        if not os.path.isdir(input_folder):
            logger.error(f"Input folder not found: {input_folder}")
            print(f"ERROR: Input folder not found: {input_folder}")
            return

        # Generate log filename for batch processing
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = os.path.join(output_folder, f"batch_processing_{timestamp}.log")

        # Create output folder
        os.makedirs(output_folder, exist_ok=True)

    else:
        # SINGLE FILE MODE
        input_file = args.input_file

        # Validate input file
        if not os.path.exists(input_file):
            logger.error(f"Input file not found: {input_file}")
            print(f"ERROR: Input file not found: {input_file}")
            return

        if not input_file.lower().endswith('.txt'):
            logger.error(f"Input file must be a .txt file: {input_file}")
            print(f"ERROR: Input file must be a .txt file: {input_file}")
            return

        # Generate output filename (same as input but with .syn extension)
        output_file = os.path.splitext(input_file)[0] + ".syn"

        # Generate log filename (same as input but with .log extension)
        log_filename = os.path.splitext(input_file)[0] + ".log"

    # Reconfigure logging
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # Create log directory if needed
    log_dir = os.path.dirname(log_filename)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    _configure_utf8_streams()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_filename, encoding="utf-8"), logging.StreamHandler()],
        force=True
    )

    # Log startup information
    logger.info("=" * 60)
    logger.info("INTERVIEW PROCESSOR STARTED")
    logger.info(f"Mode: {'BATCH' if batch_mode else 'SINGLE FILE'}")
    logger.info(f"Model: {AI_model}")
    logger.info(f"Scan mode: {args.scan_mode}")
    logger.info(f"Credit check: {'DISABLED' if args.no_credit_check else 'ENABLED'}")
    logger.info(f"Min credits threshold: ${args.min_credits:.2f} USD")
    logger.info(f"Log file: {log_filename}")
    logger.info("=" * 60)

    try:
        if batch_mode:
            # BATCH MODE
            logger.info(f"Input folder: {input_folder}")
            logger.info(f"Output folder: {output_folder}")

            # Create processor with a placeholder output file (will be changed per file)
            processor = InterviewProcessor(
                args.api_key,
                os.path.join(output_folder, "placeholder.syn"),
                args.concurrent,
                args.retries,
                scan_mode=args.scan_mode,
                min_credits_usd=args.min_credits,
                credits_check_enabled=not args.no_credit_check
            )

            # Process all files in folder
            results = await processor.process_folder(input_folder, output_folder)

            if results["success"]:
                print(f"\n✓ Batch processing complete!")
                print(f"✓ Processed {results['files_processed']}/{results['files_total']} files")
                print(f"✓ Output folder: {output_folder}")
                print(f"✓ Log file: {log_filename}")
            else:
                print(f"\n✗ Batch processing failed: {results.get('error', 'Unknown error')}")

        else:
            # SINGLE FILE MODE
            logger.info(f"Input file: {input_file}")
            logger.info(f"Output file: {output_file}")

            processor = InterviewProcessor(
                args.api_key,
                output_file,
                args.concurrent,
                args.retries,
                scan_mode=args.scan_mode,
                min_credits_usd=args.min_credits,
                credits_check_enabled=not args.no_credit_check
            )

            # Check credits before processing (single file mode)
            if not args.no_credit_check:
                success, balance, message = processor.get_api_credits()
                processor.log_credit_status(balance, message)
                if balance is not None:
                    logger.info(f"INITIAL CREDITS: ${balance:.2f} USD")
                    print(f"\n*** INITIAL API CREDITS: ${balance:.2f} USD ***\n")
                else:
                    logger.info(f"INITIAL CREDITS: {message}")
                    print(f"\n*** API STATUS: {message} ***\n")

                if not success:
                    logger.error(f"Cannot proceed: {message}")
                    print(f"\nERROR: {message}")
                    return

            await processor.process_single_file(input_file)

            # Final credit check
            if not args.no_credit_check:
                success, final_balance, message = processor.get_api_credits()
                if final_balance is not None:
                    logger.info(f"FINAL CREDITS: ${final_balance:.2f} USD")
                    print(f"\n*** FINAL API CREDITS: ${final_balance:.2f} USD ***")

            print(f"\n✓ Processing complete!")
            print(f"✓ Output saved to: {output_file}")

    except KeyboardInterrupt:
        logger.warning("Processing interrupted by user (Ctrl+C)")
        print("\n\nProcessing interrupted by user.")

    except Exception as e:
        logger.error(f"Main process failed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
