"""
Semantic Memory Builder
Extracts semantic context from DGT7 format files to create a rich JSON representation
of factors with their relationships, co-occurrences, and usage contexts.
"""

import re
import json
import argparse
import logging
import sys
from collections import defaultdict, Counter
from typing import Dict, List, Set, Tuple, Optional
from pathlib import Path

try:
    import toml
except ImportError:
    print("Warning: toml library not found. Install with: pip install toml")
    toml = None

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SemanticMemoryBuilder:
    """Builds semantic memory core from DGT7 format files."""
    
    def __init__(self, input_file: str, output_file: str):
        self.input_file = input_file
        self.output_file = output_file

        # Context capture limits tuned for token economy
        self.max_context_summary_chars = 220  # upper bound for stored snippet length
        self.max_contexts_per_factor = 3      # what we emit in JSON
        self.max_context_pool = 12            # how many scored candidates we keep internally
        
        # Data structures for each factor
        # - frequency: count of appearances (analytical value: importance/centrality in corpus)
        # - sources: unique documents mentioning factor (analytical value: breadth of evidence base)
        # - relations: typed relationships to other factors (analytical value: causal/semantic network structure)
        # - co_factors_raw: raw list for co-occurrence counting (analytical value: contextual clustering patterns)
        # - context_entries: scored summaries for later selection (analytical value: concrete usage examples)
        # - context_fingerprints: deduplication set (analytical value: prevent redundant contexts, save tokens)
        self.factors: Dict[str, Dict] = defaultdict(lambda: {
            'frequency': 0,
            'sources': set(),
            'relations': defaultdict(list),
            'co_factors_raw': [],
            'context_entries': [],
            'context_fingerprints': set()
        })
        
    def parse_dgt7_file(self) -> None:
        """Parse DGT7 format file and extract factor information."""
        logger.info(f"Reading DGT7 file: {self.input_file}")
        
        with open(self.input_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Split into document blocks
        doc_blocks = re.split(r'\[-begin_header-\]', content)
        
        for block in doc_blocks[1:]:  # Skip first empty split
            self._process_document_block(block)
        
        logger.info(f"Parsed {len(self.factors)} unique factors")

    def _summarize_context(self, context: str) -> str:
        """Compress context to a concise snippet to save tokens while keeping signal."""
        clean = ' '.join(context.split())
        if len(clean) <= self.max_context_summary_chars:
            return clean

        # Prefer the first sentence when it's within the cap; otherwise hard-truncate.
        sentence_parts = re.split(r'(?<=[.!?])\s+', clean)
        first_sentence = sentence_parts[0]
        if len(first_sentence) <= self.max_context_summary_chars:
            return first_sentence

        return first_sentence[: self.max_context_summary_chars - 3].rstrip() + "..."

    def _context_fingerprint(self, summary: str) -> str:
        """Lightweight fingerprint to deduplicate near-identical snippets."""
        return re.sub(r'\s+', ' ', summary.lower()).strip()

    def _score_context(self, summary: str, factor: str, factors: List[str], relations: List[str]) -> float:
        """
        Heuristic scoring to prioritize high-signal, informationally-rich context snippets.

        Scoring logic:
        1. Focal factor mention (+1.0): Contexts explicitly mentioning the factor provide direct evidence
        2. Neighbor factor mentions (+0.4 each): Co-mentioned factors show relational context
        3. Relation keywords (+0.2 each): Verbs like 'enables', 'constrains' indicate causal mechanisms
        4. Length penalty (-0.0 to -0.8): Prefer ~180 chars (enough detail without verbosity)
           - Very short snippets lack context
           - Very long snippets waste tokens

        Goal: Select contexts that best illustrate how the factor is actually used in literature.
        """
        lower = summary.lower()
        score = 1.0

        # Reward mention of the focal factor and its neighbors
        if factor.lower() in lower:
            score += 1.0
        score += sum(0.4 for f in factors if f != factor and f.lower() in lower)

        # Reward relation keywords when present in the snippet
        score += sum(0.2 for r in relations if r.lower() in lower)

        # Soft preference for mid-length snippets (penalize very short/very long)
        length_penalty = abs(len(summary) - 180) / 180
        score -= min(length_penalty * 0.5, 0.8)

        return max(score, 0.1)

    def _add_context_entry(self, factor: str, context: str, reference_id: str, factors: List[str], relations: List[str]) -> None:
        """Add a summarized, deduped, scored context to a factor's pool."""
        if not context:
            return

        summary = self._summarize_context(context)
        fp = self._context_fingerprint(summary)

        # Skip near-duplicates to keep variety and save space.
        if fp in self.factors[factor]['context_fingerprints']:
            return

        score = self._score_context(summary, factor, factors, relations)
        entry = {
            'summary': summary,
            'source': reference_id,
            'score': score
        }

        pool = self.factors[factor]['context_entries']
        pool.append(entry)
        self.factors[factor]['context_fingerprints'].add(fp)

        # Trim pool to the best candidates to bound memory/tokens.
        if len(pool) > self.max_context_pool:
            pool.sort(key=lambda e: e['score'], reverse=True)
            del pool[self.max_context_pool:]

    def _select_context_summaries(self, factor_data: Dict) -> List[str]:
        """Pick top scored summaries to keep the JSON compact and signal-rich."""
        entries = sorted(
            factor_data['context_entries'],
            key=lambda e: e['score'],
            reverse=True
        )
        top_entries = entries[: self.max_contexts_per_factor]
        return [e['summary'] for e in top_entries]
    
    def _process_document_block(self, block: str) -> None:
        """Process a single document block."""
        # Extract header
        header_match = re.search(r'referencia_bibtex:\s*(\S+)', block)
        if not header_match:
            return
        
        reference_id = header_match.group(1)
        
        # Extract all relation blocks
        relation_blocks = re.findall(
            r'\[-begin-\](.*?)\[-end-\]',
            block,
            re.DOTALL
        )
        
        for rel_block in relation_blocks:
            self._process_relation_block(rel_block, reference_id)
    
    def _process_relation_block(self, block: str, reference_id: str) -> None:
        """Process a single relation block to extract factors and relationships."""
        # Extract excerpt (context)
        excerpt_match = re.search(r'\[!(.*?)!\]', block)
        context = excerpt_match.group(1).strip() if excerpt_match else ""

        # Extract factor chain
        factors = re.findall(r'\[#(.*?)#\]', block)
        relations = re.findall(r'\[&(.*?)&\]', block)

        if not factors:
            return

        # Normalize factors
        factors = [self._normalize_factor(f) for f in factors]

        # Store factor information
        for i, factor in enumerate(factors):
            # Update basic stats
            self.factors[factor]['frequency'] += 1
            self.factors[factor]['sources'].add(reference_id)

            # Add a summarized, deduped context entry for later selection
            self._add_context_entry(factor, context, reference_id, factors, relations)

            # Process relations
            if i < len(relations):
                relation_type = relations[i]
                target_factor = factors[i + 1] if i + 1 < len(factors) else None

                if target_factor:
                    self.factors[factor]['relations'][relation_type].append(target_factor)
        
        # Track co-occurrence (all factors in same block appear together)
        for factor in factors:
            other_factors = [f for f in factors if f != factor]
            self.factors[factor]['co_factors_raw'].extend(other_factors)
    
    def _normalize_factor(self, factor: str) -> str:
        """Normalize factor name to canonical form."""
        # Remove extra whitespace
        factor = ' '.join(factor.split())

        # Capitalize first letter of each word for consistency
        factor = factor.title()

        return factor
    
    def build_semantic_memory(self) -> Dict:
        """Build the final semantic memory structure."""
        logger.info("Building semantic memory core...")
        
        semantic_memory = {
            'metadata': {
                'source_file': self.input_file,
                'total_factors': len(self.factors),
                'total_relations': sum(
                    sum(len(rels) for rels in factor['relations'].values())
                    for factor in self.factors.values()
                )
            },
            'factors': {}
        }
        
        for factor_name, factor_data in self.factors.items():
            # Calculate co-occurrence statistics
            co_factor_counts = Counter(factor_data['co_factors_raw'])
            
            # Separate into high and medium co-occurrence
            high_co = [f for f, count in co_factor_counts.most_common(10) 
                      if count >= factor_data['frequency'] * 0.3]
            medium_co = [f for f, count in co_factor_counts.most_common(20) 
                        if count >= factor_data['frequency'] * 0.15 and f not in high_co]
            
            # Build clean structure with only analytically valuable fields
            semantic_memory['factors'][factor_name] = {
                'frequency': factor_data['frequency'],
                'sources': len(factor_data['sources']),

                'relations': {
                    rel_type: list(set(targets))  # Remove duplicates
                    for rel_type, targets in factor_data['relations'].items()
                    if targets  # Only include non-empty relations
                },

                'co_factors': {
                    'high': high_co[:10],  # Top 10 high co-occurrence
                    'medium': medium_co[:10]  # Top 10 medium co-occurrence
                },

                'contexts': self._select_context_summaries(factor_data)
            }
        
        return semantic_memory
    
    def save_semantic_memory(self, semantic_memory: Dict) -> None:
        """Save semantic memory to JSON file."""
        logger.info(f"Saving semantic memory to: {self.output_file}")
        
        # Ensure output directory exists
        output_path = Path(self.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self.output_file, 'w', encoding='utf-8') as f:
            json.dump(semantic_memory, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Semantic memory saved successfully")
        logger.info(f"Total factors: {semantic_memory['metadata']['total_factors']}")
        logger.info(f"Total relations: {semantic_memory['metadata']['total_relations']}")
    
    def generate_summary_report(self, semantic_memory: Dict) -> None:
        """Generate a summary report of the semantic memory."""
        factors = semantic_memory['factors']
        
        # Calculate statistics
        total_factors = len(factors)
        avg_frequency = sum(f['frequency'] for f in factors.values()) / total_factors
        
        factors_with_relations = sum(1 for f in factors.values() if f['relations'])
        factors_with_contexts = sum(1 for f in factors.values() if f['contexts'])
        
        # Top factors by frequency
        top_factors = sorted(
            factors.items(),
            key=lambda x: x[1]['frequency'],
            reverse=True
        )[:10]
        
        logger.info("\n" + "="*60)
        logger.info("SEMANTIC MEMORY SUMMARY")
        logger.info("="*60)
        logger.info(f"Total unique factors: {total_factors}")
        logger.info(f"Average frequency per factor: {avg_frequency:.2f}")
        logger.info(f"Factors with relations: {factors_with_relations} ({factors_with_relations/total_factors*100:.1f}%)")
        logger.info(f"Factors with contexts: {factors_with_contexts} ({factors_with_contexts/total_factors*100:.1f}%)")
        logger.info("\nTop 10 factors by frequency:")
        for i, (name, data) in enumerate(top_factors, 1):
            logger.info(f"  {i}. {name}: {data['frequency']} occurrences")
        logger.info("="*60 + "\n")
    
    def run(self) -> None:
        """Execute the complete semantic memory building process."""
        try:
            # Parse DGT7 file
            self.parse_dgt7_file()
            
            # Build semantic memory
            semantic_memory = self.build_semantic_memory()
            
            # Save to JSON
            self.save_semantic_memory(semantic_memory)
            
            # Generate summary report
            self.generate_summary_report(semantic_memory)
            
        except Exception as e:
            logger.error(f"Error building semantic memory: {e}")
            raise


def load_config() -> Optional[Dict]:
    """Load configuration from config.toml if available."""
    if toml is None:
        return None

    try:
        config_path = Path("config.toml")
        if config_path.exists():
            logger.info("Loading configuration from config.toml")
            return toml.load(config_path)
        else:
            logger.warning("config.toml not found, using command-line arguments only")
            return None
    except Exception as e:
        logger.warning(f"Error loading config.toml: {e}")
        return None


def get_default_paths_from_config(config: Optional[Dict]) -> tuple[Optional[str], Optional[str]]:
    """Extract default input and output paths from config."""
    if config is None:
        return None, None

    # Get input file from abstract_processor.output_file
    input_file = config.get("abstract_processor", {}).get("output_file")

    # Generate output file name based on input file
    output_file = None
    if input_file:
        input_path = Path(input_file)
        # Replace .txt with _semantic_memory.json
        output_file = str(input_path.with_name(
            input_path.stem + "_semantic_memory.json"
        ))
        logger.info(f"Generated output path: {output_file}")

    return input_file, output_file


def main():
    # Load config first
    config = load_config()
    default_input, default_output = get_default_paths_from_config(config)

    parser = argparse.ArgumentParser(
        description="Build Semantic Memory Core from DGT7 format files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using command-line arguments
  python semantic_memory_builder.py --input input/data.txt --output output/memory.json

  # Using config.toml defaults (reads from [abstract_processor] output_file)
  python semantic_memory_builder.py

  # Using config.toml input but custom output
  python semantic_memory_builder.py --output custom_output.json
        """
    )

    parser.add_argument(
        '--input',
        default=default_input,
        help='Input DGT7 format TXT file (default: from config.toml [abstract_processor] output_file)'
    )
    parser.add_argument(
        '--output',
        default=default_output,
        help='Output JSON file for semantic memory (default: auto-generated from input filename)'
    )

    args = parser.parse_args()

    # Validate that we have both input and output
    if not args.input:
        parser.error("No input file specified. Either provide --input or configure [abstract_processor] output_file in config.toml")

    if not args.output:
        parser.error("No output file specified. Either provide --output or configure [abstract_processor] output_file in config.toml")

    # Check if input file exists
    if not Path(args.input).exists():
        logger.error(f"Input file not found: {args.input}")
        sys.exit(1)

    logger.info(f"Input file: {args.input}")
    logger.info(f"Output file: {args.output}")

    builder = SemanticMemoryBuilder(args.input, args.output)
    builder.run()


if __name__ == "__main__":
    main()
