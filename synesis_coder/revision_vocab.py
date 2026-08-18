"""Vocabulário único das tags de revisão do `.synr`.

Antes deste módulo os mesmos nomes de tag viviam duplicados em duas constantes
literais — `_META_TAGS` (incorporate) e `_CRITIQUE_META_TAGS` (prompt_builder) —
sem nada que as mantivesse sincronizadas. Renomear exigia lembrar das duas, e
uma divergência seria silenciosa. Ver Estudo_Assimetria_Contexto_Critique §9.3.

Duas mudanças de vocabulário convivem aqui:

1. **Nomes.** O cabeçalho falava a língua da auditoria (`suspicion`, `flagged`,
   `threshold`), inadequada a pesquisa qualitativa, cuja prática de referência é
   revisão por pares. Os nomes novos descrevem a relação revisor↔anotação.
2. **Direção.** `suspicion_rate` media na direção errada: o pesquisador quer
   MAXIMIZAR qualidade, não minimizar suspeita. `agreement = 1 - suspicion_rate`
   usa a mesma escala já adotada para comparação com padrão ouro nos demais
   estudos, e dispensa a nota "< 0.30 indica boa qualidade".

Retrocompatibilidade é obrigatória: `.synr` gerados antes desta versão (formato
1) precisam continuar legíveis por `refine` e `incorporate`. `canonical_tag()`
traduz nome antigo → novo, e os leitores sempre passam por ela.
"""

from __future__ import annotations

# Versão do formato .synr. Emitida como `# $format: 2` no cabeçalho.
# Formato 1 = sem esta chave, vocabulário antigo.
SYNR_FORMAT_VERSION = 2

# Nome antigo → nome canônico atual.
_ALIASES = {
    "suspicion_score": "divergence",
    "reason_detail": "comment",
    "suspicion_rate": "agreement",  # ATENÇÃO: valor também inverte — ver nota
    "items_flagged": "items_to_review",
    "threshold": "sensitivity",
}

# Tags de diagnóstico — descrevem a revisão, nunca são correção de campo do ITEM.
# Inclui os nomes antigos para que um .synr de formato 1 continue sendo lido
# corretamente, e o cabeçalho do próprio .synr (model/timestamp/…), que de outro
# modo seria aplicado como valor de campo homônimo do template.
META_TAGS = frozenset(
    {
        # diagnóstico (nomes atuais)
        "divergence",
        "reason",
        "comment",
        # diagnóstico (formato 1)
        "suspicion_score",
        "reason_detail",
        # o LLM usa `# $note:` como raciocínio, não como campo `note:` do ITEM
        "note",
        # cabeçalho do .synr
        "phase",
        "format",
        "model",
        "timestamp",
        "threshold",
        "sensitivity",
    }
)

# Campos cujas repetições são rascunho do modelo: só a última versão vale.
SCALAR_TAGS = frozenset({"divergence", "reason", "comment"} | set(_ALIASES))


def canonical_tag(name: str) -> str:
    """Traduz um nome de tag do formato 1 para o vocabulário atual.

    Nomes já canônicos passam inalterados, o que torna a função segura de
    aplicar em qualquer ponto de leitura.
    """
    return _ALIASES.get(name, name)


def is_meta_tag(name: str) -> bool:
    """True para tags de diagnóstico/cabeçalho — nunca correções de campo."""
    base = name.split(".")[0] if "." in name else name
    return base in META_TAGS or name.startswith("metrics.")


# --- Sensibilidade nomeada ---------------------------------------------------
# `threshold: 0.2` não comunica nada a quem não conhece a escala. Um rótulo
# nomeado carrega a intenção; o número continua aceito para quem quiser afinar.

SENSITIVITY_LEVELS = {
    "lenient": 0.35,
    "standard": 0.20,
    "strict": 0.10,
}
DEFAULT_SENSITIVITY = "standard"


def resolve_sensitivity(value: str | float | None) -> tuple[float, str]:
    """Resolve sensibilidade (rótulo ou número) para (limiar, rótulo_exibido).

    Aceita `"strict"`, `"0.15"` ou `0.15`. Um número sem rótulo correspondente
    é exibido como ele mesmo.
    """
    if value is None:
        return SENSITIVITY_LEVELS[DEFAULT_SENSITIVITY], DEFAULT_SENSITIVITY

    if isinstance(value, str):
        key = value.strip().lower()
        if key in SENSITIVITY_LEVELS:
            return SENSITIVITY_LEVELS[key], key
        try:
            numeric = float(key)
        except ValueError as exc:
            raise ValueError(
                f"Sensibilidade inválida: {value!r}. "
                f"Use {'/'.join(SENSITIVITY_LEVELS)} ou um número entre 0 e 1."
            ) from exc
    else:
        numeric = float(value)

    for label, threshold in SENSITIVITY_LEVELS.items():
        if abs(threshold - numeric) < 1e-9:
            return numeric, label
    return numeric, f"{numeric:g}"
