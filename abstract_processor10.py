import anthropic
import asyncio
import pandas as pd
from tqdm import tqdm
import argparse
import logging
from typing import List, Dict, Set, Tuple
import os
import re
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import nltk
from nltk.tokenize import word_tokenize
import json
import time
import toml
from collections import deque
from functools import lru_cache
import bibtexparser

# Load configuration from config.toml
config = toml.load("config.toml")

# Configuration constants
AI_model = config["abstract_processor"]["AI_model"]
DEFAULT_API_KEY = config["abstract_processor"]["api_key"]
DEFAULT_CONCURRENCY = int(config["abstract_processor"]["concurrent"])
DEFAULT_RETRIES = int(config["abstract_processor"]["retries"])
DEFAULT_OUTPUT = config["abstract_processor"]["output_file"]
DEFAULT_BATCH_SIZE = int(config["abstract_processor"]["batch_size"])
log_file = config["abstract_processor"]["log_file"]
# Concept files are deprecated (no longer generated)
concepts_file = config["abstract_processor"].get("concepts_file", None)
metadata_file = config["abstract_processor"].get("metadata_file", None)

print("Preparing data! Please, be patient...")

# Prompt base
system_prompt = config["prompts"]["system_prompt"]

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Ensure NLTK punkt data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    logger.info("Downloading NLTK punkt data")
    nltk.download('punkt')

class AbstractProcessor:
    """Handles processing of scientific abstracts with enhanced text handling."""

    def __init__(self, api_key: str, output_file: str, max_concurrent: int, max_retries: int):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.output_file = output_file
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        # Concept tracking removed - no longer needed
        self.semaphore = asyncio.Semaphore(max_concurrent)

        # Use deque for efficient rate limiting
        self.recent_calls = deque()
        self.rate_window = 60
        self.max_rpm = 50
        self.input_tokens_used = deque()
        self.output_tokens_used = deque()
        self.max_input_tokens_per_minute = 30000
        self.max_output_tokens_per_minute = 8000

        # Cache for token estimation
        self._token_cache = {}

        # Create static prompt parts and cache the base prompt
        self.static_prompt_parts = self._create_static_prompt_parts()
        self._cached_base_prompt = self._build_base_prompt_template()

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
            "relation_types": config["prompts"]["relation_types"],
            "critical_reminders": config["prompts"]["critical_reminders"],
            "output_format": config["prompts"]["output_format"]
        }

    def _build_base_prompt_template(self) -> str:
        """Build the static parts of the prompt once to avoid repeated string concatenation."""
        return "\n\n".join([
            self.static_prompt_parts["task"],
            "{abstract_part}",  # Placeholder for dynamic content
            self.static_prompt_parts["instructions"],
            self.static_prompt_parts["relation_types"],
            self.static_prompt_parts["output_format"],  # Now comes from config.toml
            self.static_prompt_parts["critical_reminders"],
            "Analyze the abstract and provide your analysis in the exact format specified above."
        ])

    def estimate_tokens(self, text: str) -> int:
        """Estimate tokens with caching to avoid repeated tokenization."""
        # Use hash of text as cache key
        text_hash = hash(text)
        if text_hash in self._token_cache:
            return self._token_cache[text_hash]

        try:
            # Simple estimation: ~0.75 words per token (1.3 tokens per word)
            # Avoid expensive word_tokenize, use simple split
            words = len(text.split())
            tokens = int(words * 1.3)
            self._token_cache[text_hash] = tokens
            return tokens
        except Exception as e:
            logger.warning(f"Token estimation failed: {e}")
            fallback = len(text) // 4
            self._token_cache[text_hash] = fallback
            return fallback

    def truncate_abstract(self, abstract: str, max_tokens: int = 15000) -> str:
        """Truncates abstract to fit within token limit while preserving meaning."""
        estimated_tokens = self.estimate_tokens(abstract)
        if estimated_tokens <= max_tokens:
            return abstract
        
        logger.info(f"Truncating abstract: {estimated_tokens} tokens to {max_tokens}")
        sentences = nltk.sent_tokenize(abstract)
        truncated = ""
        current_tokens = 0
        
        for sentence in sentences:
            sentence_tokens = self.estimate_tokens(sentence)
            if current_tokens + sentence_tokens <= max_tokens:
                truncated += sentence + " "
                current_tokens += sentence_tokens
            else:
                break
        
        return truncated.strip()

    def create_prompt(self, reference_id: str, abstract: str) -> str:
        """Create prompt using cached template for better performance."""
        # Truncate abstract if necessary
        abstract = self.truncate_abstract(abstract)

        abstract_part = f"""<abstract>
reference_id: {reference_id}

{abstract}
</abstract>"""

        # First replace {reference_id} placeholders in the cached template
        prompt_with_ref_id = self._cached_base_prompt.replace("{reference_id}", reference_id)

        # Then format with the abstract_part
        prompt = prompt_with_ref_id.format(abstract_part=abstract_part)

        return prompt

    def should_throttle(self) -> bool:
        """Efficient rate limiting using deque."""
        now = time.time()
        # Remove old entries from the left (oldest)
        while self.recent_calls and now - self.recent_calls[0] >= self.rate_window:
            self.recent_calls.popleft()
        return len(self.recent_calls) >= self.max_rpm

    def check_token_budget(self, input_tokens: int, max_output_tokens: int) -> bool:
        """Efficient token budget checking using deque."""
        now = time.time()

        # Remove old entries from input tokens
        while self.input_tokens_used and now - self.input_tokens_used[0][0] >= self.rate_window:
            self.input_tokens_used.popleft()

        # Remove old entries from output tokens
        while self.output_tokens_used and now - self.output_tokens_used[0][0] >= self.rate_window:
            self.output_tokens_used.popleft()

        current_input_usage = sum(tokens for _, tokens in self.input_tokens_used)
        current_output_usage = sum(tokens for _, tokens in self.output_tokens_used)

        return (current_input_usage + input_tokens <= self.max_input_tokens_per_minute and
                current_output_usage + max_output_tokens <= self.max_output_tokens_per_minute)

    def extract_header(self, response: str) -> str:
        match = re.search(r'\[-begin_header-\](.*?)\[-end_header-\]', response, re.DOTALL)
        return f"[-begin_header-]{match.group(1).strip()}[-end_header-]" if match else ""

    def extract_blocks(self, response: str) -> List[str]:
        blocks = re.findall(r'\[-begin-\]\s*(.*?)\s*\[-end-\]', response, re.DOTALL)
        fixed_blocks = []
        for block in blocks:
            ref_match = re.search(r'\[@(.*?)@\]', block)
            if not ref_match:
                logger.warning(f"Missing reference ID in block: {block}")
                continue
            
            ref_id = ref_match.group(1)
            fixed_block = re.sub(r'\[@' + re.escape(ref_id) + r'@\](\[@' + re.escape(ref_id) + r'(@)?\])+', 
                               f"[@{ref_id}@]", block)
            fixed_block = re.sub(r'!\]!\]', '!]', fixed_block)
            
            excerpt_start = fixed_block.find(f"[@{ref_id}@]") + len(f"[@{ref_id}@]")
            desc_start = fixed_block.find('[%')
            
            if desc_start == -1:
                logger.warning(f"Missing description in block: {fixed_block}")
                continue
                
            excerpt_text = fixed_block[excerpt_start:desc_start].strip()
            if not (excerpt_text.startswith('[!') and excerpt_text.endswith('!]')):
                excerpt_text = excerpt_text.replace('[!', '').replace('!]', '')
                new_excerpt = f"[!{excerpt_text}!]"
                fixed_block = fixed_block[:excerpt_start] + new_excerpt + fixed_block[desc_start:]
            
            fixed_blocks.append(fixed_block)
        
        return fixed_blocks

    # extract_concepts method removed - concept tracking no longer needed

    def split_large_abstract(self, abstract: str, max_chars=30000) -> List[str]:
        """Splits a large abstract into smaller chunks at sentence boundaries."""
        if len(abstract) <= max_chars:
            return [abstract]
        
        # Split at sentence boundaries
        sentences = re.split(r'(?<=[.!?])\s+', abstract)
        chunks = []
        current_chunk = ""
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) > max_chars:
                if current_chunk:  # Save the current chunk
                    chunks.append(current_chunk)
                    current_chunk = sentence
                else:  # Edge case: single sentence exceeds max_chars
                    chunks.append(sentence)
            else:
                current_chunk += (" " if current_chunk else "") + sentence
        
        # Add the last chunk if it's not empty
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks

    async def _process_single_chunk(self, reference_id: str, abstract: str) -> Tuple[str, str]:
        """Process a single abstract chunk with retries and rate limiting."""
        # Throttle if nearing rate limit
        while self.should_throttle():
            logger.warning(f"Rate limit approaching for {reference_id}, waiting...")
            await asyncio.sleep(3)
        
        # Build prompt and estimate tokens
        prompt = self.create_prompt(reference_id, abstract)
        input_tokens = self.estimate_tokens(prompt)
        max_output_tokens = min(4000, max(2000, input_tokens))
        
        # Wait if token budget is exceeded
        while not self.check_token_budget(input_tokens, max_output_tokens):
            logger.warning(f"Token budget exceeded for {reference_id}, waiting...")
            await asyncio.sleep(6)
        
        # Track this API call
        now = time.time()
        self.recent_calls.append(now)
        self.input_tokens_used.append((now, input_tokens))
        self.output_tokens_used.append((now, max_output_tokens))
        
        # Make the API call with a timeout
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.messages.create,
                    model=AI_model,
                    max_tokens=max_output_tokens,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}]
                ),
                timeout=120
            )
            content = response.content[0].text
            logger.info(f"Received response for {reference_id}")
        except asyncio.TimeoutError:
            logger.error(f"API call timed out for {reference_id}")
            raise anthropic.APITimeoutError("Request timed out")
        
        # Validate and fix response format
        validated_content = self.validate_response_format(reference_id, content)
        logger.info(f"Successfully processed {reference_id}")

        return reference_id, validated_content

    def combine_chunk_outputs(self, original_ref_id: str, chunk_outputs: List[str]) -> str:
        """Combines outputs from multiple chunks into a single coherent result."""
        if not chunk_outputs:
            return f"[-begin_header-]\nreferencia_bibtex:{original_ref_id}\ndescricao:[Analysis could not be completed]\nmetodo:[Not applicable]\n[-end_header-]"
        
        # Extract header from first chunk as base
        combined_header = self.extract_header(chunk_outputs[0])
        if not combined_header:
            combined_header = f"[-begin_header-]\nreferencia_bibtex:{original_ref_id}\ndescricao:[Combined analysis from multiple chunks]\nmetodo:[Combined analysis]\n[-end_header-]"
        
        # Replace any chunk reference IDs with the original
        combined_header = re.sub(r'referencia_bibtex:.*?_part\d+', f'referencia_bibtex:{original_ref_id}', combined_header)
        
        # Collect all blocks from all chunks
        all_blocks = []
        for output in chunk_outputs:
            blocks = self.extract_blocks(output)
            for block in blocks:
                # Replace any chunk reference IDs with the original
                block = re.sub(r'\[@.*?_part\d+@\]', f'[@{original_ref_id}@]', block)
                all_blocks.append(block)
        
        # Combine everything
        combined_output = combined_header + "\n\n" + "\n\n".join([f"[-begin-]{block}[-end-]" for block in all_blocks])
        return combined_output

    def _flush_output_buffer(self) -> None:
        """Write buffered results to file."""
        if self.output_buffer:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write("\n\n".join(self.output_buffer) + "\n\n")
            logger.info(f"Flushed {len(self.output_buffer)} results to file")
            self.output_buffer.clear()

    def validate_response_format(self, reference_id: str, content: str) -> str:
        # Check for "NO EXTRACTABLE CHAINS" message first
        if "NO EXTRACTABLE CHAINS" in content:
            # Extract the full reason message
            no_chains_match = re.search(r'NO EXTRACTABLE CHAINS[:\s-]*([^\n]+)', content)
            reason = no_chains_match.group(1).strip() if no_chains_match else "Abstract does not contain relevant content"

            # Clean up markdown formatting characters
            reason = reason.replace('**', '').replace('*', '').strip()

            logger.info(f"No extractable chains for {reference_id}: {reason}")

            # Create a properly formatted output with the reason
            header = f"[-begin_header-]\nreferencia_bibtex:{reference_id}\ndescricao:[No extractable chains: {reason}]\nmetodo:[Not applicable]\n[-end_header-]"
            block = f"[-begin-][@{reference_id}@][!NO EXTRACTABLE CHAINS!][%{reason}%][#No Analysis#][-end-]"

            return header + "\n\n" + block

        header = self.extract_header(content)
        blocks = self.extract_blocks(content)

        if not blocks or not header:
            logger.warning(f"Format issues for {reference_id}, attempting recovery")
            if "[#" in content and "[&" in content:
                if not header:
                    header = f"[-begin_header-]\nreferencia_bibtex:{reference_id}\ndescricao:[Abstract on energy technology acceptance]\nmetodo:[Not specified]\n[-end_header-]"
                
                potential_blocks = []
                content_parts = re.split(r'\[-begin-\]|\[-end-\]', content)
                
                for part in content_parts:
                    if "[#" in part and "[&" in part:
                        if not f"[@{reference_id}@]" in part:
                            part = f"[@{reference_id}@]{part}"
                        if not "[!" in part:
                            desc_idx = part.find('[%')
                            if desc_idx > 0:
                                ref_end = part.find('@]') + 2
                                excerpt_text = part[ref_end:desc_idx].strip()
                                part = part[:ref_end] + f"[!{excerpt_text}!]" + part[desc_idx:]
                        potential_blocks.append(part)
                
                if potential_blocks:
                    blocks = potential_blocks
        
        validated_blocks = []
        for block in blocks:
            if block.count(f"[@{reference_id}") > 1:
                block = re.sub(r'\[@' + re.escape(reference_id) + r'@\](\[@' + re.escape(reference_id) + r'(@)?\])+', 
                                f"[@{reference_id}@]", block)
            if not f"[@{reference_id}@]" in block:
                block = f"[@{reference_id}@]{block}"
            if '!]!]' in block:
                block = block.replace('!]!]', '!]')
            if not re.search(r'\[!.*?!\]', block):
                desc_idx = block.find('[%')
                if desc_idx > 0:
                    ref_end = block.find('@]') + 2
                    excerpt_text = block[ref_end:desc_idx].strip()
                    block = block[:ref_end] + f"[!{excerpt_text}!]" + block[desc_idx:]
            validated_blocks.append(block)
        
        if header and validated_blocks:
            output = header + "\n\n" + "\n\n".join([f"[-begin-]{block}[-end-]" for block in validated_blocks])
        elif validated_blocks:
            output = "\n\n".join([f"[-begin-]{block}[-end-]" for block in validated_blocks])
        else:
            output = f"[-begin_header-]\nreferencia_bibtex:{reference_id}\ndescricao:[Analysis could not be completed]\nmetodo:[Not applicable]\n[-end_header-]\n\n[-begin-][@{reference_id}@][!No valid analysis generated!][%Format issues in response%][#Format Error#][&causes&][#Missing Analysis#][-end-]"
        
        return output

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=120),
        stop=stop_after_attempt(7),
        retry=retry_if_exception_type((
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError
        ))
    )
    async def process_abstract(self, reference_id: str, abstract: str) -> Tuple[str, str]:
        """Processes a single abstract asynchronously with retries and rate limiting."""
        async with self.semaphore:
            logger.info(f"Processing abstract for {reference_id}")
            
            # Split large abstracts into manageable chunks
            abstract_chunks = self.split_large_abstract(abstract)
            if len(abstract_chunks) > 1:
                logger.info(f"Split large abstract {reference_id} into {len(abstract_chunks)} chunks")

                # Process each chunk separately and combine results
                all_outputs = []

                for i, chunk in enumerate(abstract_chunks):
                    chunk_ref = f"{reference_id}_part{i+1}"
                    try:
                        _, chunk_output = await self._process_single_chunk(chunk_ref, chunk)
                        all_outputs.append(chunk_output)
                    except Exception as e:
                        logger.error(f"Failed to process chunk {i+1} of {reference_id}: {e}")

                # Combine the outputs into a single result
                combined_output = self.combine_chunk_outputs(reference_id, all_outputs)
                return reference_id, combined_output
            
            # For regular-sized abstracts, process normally
            return await self._process_single_chunk(reference_id, abstract)

    async def _process_batch(self, batch: List[Dict[str, str]], pbar=None) -> None:
        tasks = [
            self.process_abstract(item['Reference'], item['Abstract'])
            for item in batch if item.get('Abstract', "").strip()
        ]

        if not tasks:
            logger.warning("No valid abstracts in this batch")
            return

        results = []
        for task in asyncio.as_completed(tasks):
            try:
                ref_id, output = await task
                results.append((ref_id, output))
                if pbar:
                    pbar.update(1)
            except Exception as e:
                logger.error(f"Failed to complete a task in batch: {e}")

        # Process results with buffered file writes
        for ref_id, output in results:
            self.output_buffer.append(output)

            # Flush buffer when it reaches the buffer size
            if len(self.output_buffer) >= self.buffer_size:
                self._flush_output_buffer()

    async def process_all(self, abstracts: List[Dict[str, str]], batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        valid_abstracts = [item for item in abstracts if item.get('Abstract', "").strip()]
        
        if not valid_abstracts:
            logger.warning("No valid abstracts to process")
            return
            
        total_batches = (len(valid_abstracts) + batch_size - 1) // batch_size
        
        with tqdm(total=len(valid_abstracts), desc="Processing Abstracts") as pbar:
            for i in range(0, len(valid_abstracts), batch_size):
                batch = valid_abstracts[i:i + batch_size]
                logger.info(f"Processing batch {i//batch_size + 1}/{total_batches} with {len(batch)} abstracts")
                
                batch_start = time.time()
                await self._process_batch(batch, pbar)
                
                batch_time = time.time() - batch_start
                logger.info(f"Batch {i//batch_size + 1} completed in {batch_time:.2f} seconds")
                
                if i + batch_size < len(valid_abstracts):
                    cooldown = max(10, min(60, batch_time * 0.7))
                    logger.info(f"Cooling down for {cooldown:.1f} seconds before next batch")
                    await asyncio.sleep(cooldown)

        # Flush any remaining buffered results
        self._flush_output_buffer()

        # Concept file generation removed - no longer needed
        logger.info(f"Processed {len(valid_abstracts)} abstracts successfully.")
        logger.info(f"Results saved to {self.output_file}")

def load_bibtex(file_path: str) -> List[Dict[str, str]]:
    """Load abstracts from BibTeX file using bibtexparser.

    Args:
        file_path: Path to .bib file

    Returns:
        List of dictionaries with 'Reference' and 'Abstract' keys
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as bibfile:
            bib_database = bibtexparser.load(bibfile)

        abstracts = []
        for entry in bib_database.entries:
            reference = entry.get('ID', '')
            abstract = entry.get('abstract', '')

            if reference and abstract:
                abstracts.append({
                    'Reference': reference,
                    'Abstract': abstract
                })
            else:
                logger.warning(f"Entry {reference or 'unknown'} missing ID or abstract")

        return abstracts

    except Exception as e:
        logger.error(f"Failed to load BibTeX file {file_path}: {e}")
        raise

async def main():
    parser = argparse.ArgumentParser(description="Process scientific abstracts using Claude API")
    parser.add_argument(
        "--input",
        default=config["abstract_processor"]["input_file"],
        help="Input CSV with Reference and Abstract columns"
    )
    parser.add_argument(
        "--output",
        default=config["abstract_processor"]["output_file"],
        help="Output file path"
    )
    parser.add_argument(
        "--api-key",
        default=config["abstract_processor"]["api_key"],
        help="Claude API key"
    )
    parser.add_argument(
        "--concurrent",
        type=int,
        default=int(config["abstract_processor"]["concurrent"]),
        help="Max concurrent requests"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=int(config["abstract_processor"]["retries"]),
        help="Max retries per request"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(config["abstract_processor"]["batch_size"]),
        help="Batch size for processing"
    )
    args = parser.parse_args()

    try:
        logger.info(f"Loading data from {args.input}")

        # Load BibTeX file
        abstracts = load_bibtex(args.input)

        logger.info(f"Loaded {len(abstracts)} abstracts from {args.input}")
        processor = AbstractProcessor(args.api_key, args.output, args.concurrent, args.retries)
        await processor.process_all(abstracts, args.batch_size)

    except Exception as e:
        logger.error(f"Main process failed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())