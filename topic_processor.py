"""
Topic Processor
Classifies factors from semantic memory into topics, aspects, and dimensions using Claude API.
Adapted from abstract_processor9.py for factor classification instead of abstract processing.
"""

import anthropic
import asyncio
import json
import logging
import argparse
import toml
import csv
from pathlib import Path
from typing import Dict, List, Tuple
from collections import deque
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import time
import re

# Load configuration
config = toml.load("config.toml")

# Configuration constants
AI_model = config["topic_processor"]["AI_model"]
DEFAULT_API_KEY = config["topic_processor"]["api_key"]
DEFAULT_CONCURRENCY = int(config["topic_processor"]["concurrent"])
DEFAULT_RETRIES = int(config["topic_processor"]["retries"])
DEFAULT_BATCH_SIZE = int(config["topic_processor"]["batch_size"])
DEFAULT_INPUT = config["topic_processor"]["input_file"]
DEFAULT_OUTPUT = config["topic_processor"]["output_file"]
log_file = config["topic_processor"]["log_file"]

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class TopicProcessor:
    """Processes factors with semantic context to classify them into topics."""
    
    def __init__(self, api_key: str, output_file: str, max_concurrent: int, max_retries: int):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.output_file = output_file
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(max_concurrent)
        
        # Rate limiting
        self.recent_calls = deque()
        self.rate_window = 60
        self.max_rpm = 50
        self.input_tokens_used = deque()
        self.output_tokens_used = deque()
        self.max_input_tokens_per_minute = 30000
        self.max_output_tokens_per_minute = 8000
        
        # Load prompts from config
        self.system_prompt = config["topic_prompts"]["system_prompt"]
        self.classification_prompt_template = config["topic_prompts"]["classification_prompt"]
        
        # Results storage
        self.results = []
        
        # Progress tracking
        self.start_time = None
        self.total_factors = 0
        self.processed_count = 0
        self.last_progress_log = 0.0
        
        # Ensure output directory exists
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    
    def should_throttle(self) -> bool:
        """Check if we should throttle requests."""
        now = time.time()
        while self.recent_calls and now - self.recent_calls[0] >= self.rate_window:
            self.recent_calls.popleft()
        return len(self.recent_calls) >= self.max_rpm
    
    def check_token_budget(self, input_tokens: int, max_output_tokens: int) -> bool:
        """Check if we have token budget available."""
        now = time.time()
        
        while self.input_tokens_used and now - self.input_tokens_used[0][0] >= self.rate_window:
            self.input_tokens_used.popleft()
        
        while self.output_tokens_used and now - self.output_tokens_used[0][0] >= self.rate_window:
            self.output_tokens_used.popleft()
        
        current_input_usage = sum(tokens for _, tokens in self.input_tokens_used)
        current_output_usage = sum(tokens for _, tokens in self.output_tokens_used)
        
        return (current_input_usage + input_tokens <= self.max_input_tokens_per_minute and
                current_output_usage + max_output_tokens <= self.max_output_tokens_per_minute)
    
    def create_classification_prompt(self, factor_name: str, factor_context: Dict) -> str:
        """Create classification prompt with factor context."""
        # Format context as readable text
        context_text = self._format_context(factor_name, factor_context)
        
        # Insert context into prompt template
        prompt = self.classification_prompt_template.replace("{{factor_context}}", context_text)
        
        return prompt
    
    def _format_context(self, factor_name: str, context: Dict) -> str:
        """Format factor context into readable text for the prompt."""
        parts = [f"## Factor: {factor_name}\n"]
        
        # Basic stats
        parts.append(f"**Frequency**: {context.get('frequency', 0)} occurrences")
        parts.append(f"**Sources**: {context.get('sources', 0)} documents\n")
        
        # Relations
        relations = context.get('relations', {})
        if relations:
            parts.append("**Relations**:")
            for rel_type, targets in relations.items():
                if targets:
                    parts.append(f"  - {rel_type}: {', '.join(targets[:5])}")
            parts.append("")
        
        # Co-factors
        co_factors = context.get('co_factors', {})
        if co_factors:
            parts.append("**Co-occurring Factors**:")
            if co_factors.get('high'):
                parts.append(f"  - High co-occurrence: {', '.join(co_factors['high'][:8])}")
            if co_factors.get('medium'):
                parts.append(f"  - Medium co-occurrence: {', '.join(co_factors['medium'][:8])}")
            parts.append("")
        
        # Contexts (examples)
        contexts = context.get('contexts', [])
        if contexts:
            parts.append("**Usage Contexts**:")
            for i, ctx in enumerate(contexts[:3], 1):
                parts.append(f"  {i}. \"{ctx}\"")
            parts.append("")

        return "\n".join(parts)
    
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
    async def classify_factor(self, factor_name: str, factor_context: Dict) -> Dict:
        """Classify a single factor using Claude API."""
        async with self.semaphore:
            logger.info(f"Classifying factor: {factor_name}")
            
            # Create prompt
            prompt = self.create_classification_prompt(factor_name, factor_context)
            
            # Estimate tokens
            estimated_input_tokens = len(prompt) // 4
            estimated_output_tokens = 300  # Conservative estimate for classification output
            
            # Wait for rate limiting
            while self.should_throttle() or not self.check_token_budget(estimated_input_tokens, estimated_output_tokens):
                logger.debug("Rate limiting or token budget exceeded, waiting...")
                await asyncio.sleep(2)
            
            # Make API call
            try:
                self.recent_calls.append(time.time())
                
                response = await asyncio.to_thread(
                    self.client.messages.create,
                    model=AI_model,
                    max_tokens=2000,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": prompt}]
                )
                
                # Track token usage
                now = time.time()
                self.input_tokens_used.append((now, response.usage.input_tokens))
                self.output_tokens_used.append((now, response.usage.output_tokens))
                
                # Extract response
                response_text = response.content[0].text.strip()
                
                # Parse response
                classification = self._parse_classification(factor_name, response_text)
                
                logger.info(f"Successfully classified: {factor_name} -> {classification['topic']}")
                return classification
                
            except Exception as e:
                logger.error(f"Error classifying {factor_name}: {e}")
                raise
    
    def _parse_classification(self, factor_name: str, response_text: str) -> Dict:
        """Parse Claude's classification response."""
        # Try to extract CSV line with RGT fields
        csv_match = re.search(
            r'([^,]+),([^,]+),(\d+),(\d+),(LOW|MEDIUM|HIGH),"([^"]+)","([^"]+)","([^"]+)","([^"]+)"',
            response_text
        )

        if csv_match:
            return {
                'factor': factor_name,
                'topic': csv_match.group(2).strip(),
                'aspect': int(csv_match.group(3)),
                'dimension': int(csv_match.group(4)),
                'confidence': csv_match.group(5).strip(),
                'reasoning': csv_match.group(6).strip(),
                'factor_description': csv_match.group(7).strip(),
                'rgt_element_a': csv_match.group(8).strip(),
                'rgt_element_b': csv_match.group(9).strip(),
                'theorethical_significance': 0
            }

        # Try old format without RGT fields
        csv_match_old = re.search(
            r'([^,]+),([^,]+),(\d+),(\d+),(LOW|MEDIUM|HIGH),"([^"]+)","([^"]+)"',
            response_text
        )

        if csv_match_old:
            result = {
                'factor': factor_name,
                'topic': csv_match_old.group(2).strip(),
                'aspect': int(csv_match_old.group(3)),
                'dimension': int(csv_match_old.group(4)),
                'confidence': csv_match_old.group(5).strip(),
                'reasoning': csv_match_old.group(6).strip(),
                'factor_description': csv_match_old.group(7).strip()
            }
            # Generate RGT fields if not present
            rgt_a, rgt_b = self._generate_rgt_construct(factor_name)
            result['rgt_element_a'] = rgt_a
            result['rgt_element_b'] = rgt_b
            result['theorethical_significance'] = 0
            return result

        # Fallback: try to extract fields individually
        logger.warning(f"Could not parse CSV format for {factor_name}, attempting field extraction")

        topic = self._extract_field(response_text, ['topic', 'thematic_group', 'category'])
        aspect = self._extract_number(response_text, ['aspect'])
        dimension = self._extract_number(response_text, ['dimension'])
        confidence = self._extract_field(response_text, ['confidence'], default='MEDIUM')
        reasoning = self._extract_field(response_text, ['reasoning'], default='Classification based on available context')
        description = self._extract_field(response_text, ['description', 'factor_description'], default='No description provided')

        # Try to extract RGT fields, or generate them
        rgt_a = self._extract_field(response_text, ['rgt_element_a', 'rgt_a', 'pole_a'])
        rgt_b = self._extract_field(response_text, ['rgt_element_b', 'rgt_b', 'pole_b'])

        if not rgt_a or not rgt_b:
            rgt_a, rgt_b = self._generate_rgt_construct(factor_name)

        return {
            'factor': factor_name,
            'topic': topic or 'Uncategorized',
            'aspect': aspect if aspect is not None else 0,
            'dimension': dimension if dimension is not None else 0,
            'confidence': confidence,
            'reasoning': reasoning,
            'factor_description': description,
            'rgt_element_a': rgt_a,
            'rgt_element_b': rgt_b,
            'theorethical_significance': 0
        }

    def _generate_rgt_construct(self, factor: str) -> Tuple[str, str]:
        """
        Generate RGT bipolar construct from factor name.
        Returns (pole_a, pole_b) representing opposite poles of the construct.
        """
        factor_lower = factor.lower()

        # Common psychological/social patterns with explicit poles
        bipolar_patterns = {
            # Acceptance/Resistance
            'acceptance': ('High Acceptance', 'Low Acceptance'),
            'resistance': ('Low Resistance', 'High Resistance'),
            'support': ('Strong Support', 'Weak Support'),
            'opposition': ('Low Opposition', 'High Opposition'),

            # Trust/Confidence
            'trust': ('High Trust', 'Low Trust'),
            'confidence': ('High Confidence', 'Low Confidence'),
            'credibility': ('High Credibility', 'Low Credibility'),
            'legitimacy': ('High Legitimacy', 'Low Legitimacy'),

            # Knowledge/Awareness
            'knowledge': ('High Knowledge', 'Low Knowledge'),
            'awareness': ('High Awareness', 'Low Awareness'),
            'information': ('Well Informed', 'Poorly Informed'),
            'education': ('High Education', 'Low Education'),

            # Engagement/Participation
            'engagement': ('High Engagement', 'Low Engagement'),
            'participation': ('Active Participation', 'Low Participation'),
            'involvement': ('High Involvement', 'Low Involvement'),

            # Economic
            'cost': ('Low Cost', 'High Cost'),
            'benefit': ('High Benefit', 'Low Benefit'),
            'value': ('High Value', 'Low Value'),
            'price': ('Low Price', 'High Price'),
            'financing': ('Easy Financing Access', 'Difficult Financing Access'),
            'investment': ('High Investment', 'Low Investment'),

            # Environmental
            'impact': ('Positive Impact', 'Negative Impact'),
            'environment': ('Environmental Protection', 'Environmental Degradation'),
            'sustainability': ('High Sustainability', 'Low Sustainability'),

            # Governance/Policy
            'governance': ('Effective Governance', 'Ineffective Governance'),
            'policy': ('Clear Policy Framework', 'Unclear Policy Framework'),
            'regulation': ('Adequate Regulation', 'Inadequate Regulation'),

            # Risk/Safety
            'risk': ('Low Risk', 'High Risk'),
            'safety': ('High Safety', 'Low Safety'),
            'security': ('High Security', 'Low Security'),

            # Quality/Effectiveness
            'quality': ('High Quality', 'Low Quality'),
            'effectiveness': ('High Effectiveness', 'Low Effectiveness'),
            'efficiency': ('High Efficiency', 'Low Efficiency'),
            'performance': ('High Performance', 'Low Performance'),

            # Spatial/Accessibility
            'proximity': ('Near Proximity', 'Distant Proximity'),
            'accessibility': ('High Accessibility', 'Low Accessibility'),
            'distance': ('Short Distance', 'Long Distance'),

            # Temporal
            'experience': ('Extensive Experience', 'Limited Experience'),
            'familiarity': ('High Familiarity', 'Low Familiarity'),

            # Social
            'justice': ('High Justice', 'Low Justice'),
            'fairness': ('High Fairness', 'Low Fairness'),
            'equity': ('High Equity', 'Low Equity'),
            'transparency': ('High Transparency', 'Low Transparency'),

            # Independence/Dependence
            'independence': ('High Independence', 'High Dependence'),
            'autonomy': ('High Autonomy', 'Low Autonomy'),
        }

        # Check if factor matches known patterns
        for keyword, (pole_a, pole_b) in bipolar_patterns.items():
            if keyword in factor_lower:
                return (pole_a, pole_b)

        # If no pattern matches, create generic bipolar construct
        # Default: High/Strong vs Low/Weak pattern
        if any(word in factor_lower for word in ['cost', 'price', 'risk', 'barrier', 'constraint', 'opposition', 'resistance']):
            # Negative factors: low is positive pole
            return (f'Low {factor}', f'High {factor}')
        else:
            # Positive factors: high is positive pole
            return (f'High {factor}', f'Low {factor}')

    
    def _extract_field(self, text: str, field_names: List[str], default: str = '') -> str:
        """Extract a field value from text."""
        for field in field_names:
            pattern = rf'{field}[:\s]+([^\n,]+)'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().strip('"\'')
        return default
    
    def _extract_number(self, text: str, field_names: List[str]) -> int:
        """Extract a numeric field from text."""
        for field in field_names:
            pattern = rf'{field}[:\s]+(\d+)'
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None
    
    def _format_duration(self, seconds: float) -> str:
        """Format seconds into a human-readable duration."""
        seconds = int(max(0, seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"
    
    def _log_progress(self) -> None:
        """Log progress with elapsed time and ETA."""
        if not self.start_time or not self.total_factors:
            return
        
        now = time.time()
        processed = self.processed_count
        total = self.total_factors
        elapsed = now - self.start_time
        
        # Throttle logs to avoid excessive output unless we just finished
        if processed < total and now - self.last_progress_log < 5:
            return
        
        percent = (processed / total) * 100 if total else 0
        throughput = (processed / elapsed) if elapsed > 0 else 0
        
        if throughput > 0:
            estimated_total_duration = total / throughput
            remaining = max(0, estimated_total_duration - elapsed)
            eta_timestamp = self.start_time + estimated_total_duration
            eta_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(eta_timestamp))
            remaining_str = self._format_duration(remaining)
        else:
            eta_str = "--"
            remaining_str = "--"
        
        elapsed_str = self._format_duration(elapsed)
        logger.info(
            f"Progress: {processed}/{total} ({percent:.1f}%) | "
            f"Elapsed: {elapsed_str} | Remaining: {remaining_str} | ETA: {eta_str}"
        )
        self.last_progress_log = now
    
    async def process_batch(self, batch: List[Tuple[str, Dict]]) -> List[Dict]:
        """Process a batch of factors."""
        tasks = [
            self.classify_factor(factor_name, factor_context)
            for factor_name, factor_context in batch
        ]
        
        results = []
        for task in asyncio.as_completed(tasks):
            try:
                result = await task
                results.append(result)
                self.processed_count += 1
                self._log_progress()
            except Exception as e:
                logger.error(f"Failed to complete classification task: {e}")
        
        return results
    
    async def process_all(self, factors: Dict[str, Dict], batch_size: int) -> None:
        """Process all factors in batches."""
        factor_items = list(factors.items())
        total_factors = len(factor_items)
        total_batches = (total_factors + batch_size - 1) // batch_size
        
        # Initialize progress tracking
        self.total_factors = total_factors
        self.processed_count = 0
        self.start_time = time.time()
        self.last_progress_log = 0.0
        
        logger.info(f"Processing {total_factors} factors in {total_batches} batches")
        logger.info(f"Processing started at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.start_time))}")
        
        for i in range(0, total_factors, batch_size):
            batch = factor_items[i:i + batch_size]
            batch_num = i // batch_size + 1
            
            logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} factors)")
            
            batch_start = time.time()
            batch_results = await self.process_batch(batch)
            self.results.extend(batch_results)
            
            batch_time = time.time() - batch_start
            logger.info(f"Batch {batch_num} completed in {batch_time:.2f} seconds")
            
            # Cooldown between batches
            if i + batch_size < total_factors:
                cooldown = max(10, min(30, batch_time * 0.5))
                logger.info(f"Cooling down for {cooldown:.1f} seconds")
                await asyncio.sleep(cooldown)
        
        logger.info(f"Completed processing all {len(self.results)} factors")
    
    def save_results(self) -> None:
        """Save classification results to CSV."""
        logger.info(f"Saving results to {self.output_file}")

        with open(self.output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'factor', 'topic', 'aspect', 'dimension',
                'confidence', 'reasoning', 'factor_description',
                'rgt_element_a', 'rgt_element_b', 'theorethical_significance'
            ])
            writer.writeheader()
            writer.writerows(self.results)

        logger.info(f"Results saved successfully: {len(self.results)} classifications")

        # Generate summary
        self._generate_summary()
    
    def _generate_summary(self) -> None:
        """Generate summary statistics."""
        topics = {}
        for result in self.results:
            topic = result['topic']
            topics[topic] = topics.get(topic, 0) + 1
        
        logger.info("\n" + "="*60)
        logger.info("CLASSIFICATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Total factors classified: {len(self.results)}")
        logger.info(f"Unique topics: {len(topics)}")
        logger.info("\nTop topics:")
        for topic, count in sorted(topics.items(), key=lambda x: x[1], reverse=True)[:10]:
            logger.info(f"  {topic}: {count}")
        logger.info("="*60 + "\n")


async def main():
    parser = argparse.ArgumentParser(
        description="Classify factors from semantic memory using Claude API"
    )
    parser.add_argument(
        '--input',
        default=DEFAULT_INPUT,
        help='Input semantic memory JSON file'
    )
    parser.add_argument(
        '--output',
        default=DEFAULT_OUTPUT,
        help='Output CSV file'
    )
    parser.add_argument(
        '--api-key',
        default=DEFAULT_API_KEY,
        help='Claude API key'
    )
    parser.add_argument(
        '--concurrent',
        type=int,
        default=DEFAULT_CONCURRENCY,
        help='Max concurrent requests'
    )
    parser.add_argument(
        '--retries',
        type=int,
        default=DEFAULT_RETRIES,
        help='Max retries per request'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help='Batch size for processing'
    )
    
    args = parser.parse_args()
    
    try:
        # Load semantic memory
        logger.info(f"Loading semantic memory from {args.input}")
        with open(args.input, 'r', encoding='utf-8') as f:
            semantic_memory = json.load(f)
        
        factors = semantic_memory.get('factors', {})
        logger.info(f"Loaded {len(factors)} factors")
        
        if not factors:
            logger.error("No factors found in semantic memory")
            return
        
        # Create processor and run
        processor = TopicProcessor(
            args.api_key,
            args.output,
            args.concurrent,
            args.retries
        )
        
        await processor.process_all(factors, args.batch_size)
        processor.save_results()
        
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
