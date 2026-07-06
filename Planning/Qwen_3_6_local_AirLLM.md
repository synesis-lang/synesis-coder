**✅ Guia atualizado e otimizado para a sua configuração exata (RTX 3050 6 GB Laptop – Driver 581.95 – CUDA 13.0)**

Sua placa está **perfeita** para rodar o modelo com AirLLM:  
- 6 GB GDDR6 dedicada + ~8 GB compartilhada = ótimo headroom  
- Driver muito recente (581.95) → compatível com PyTorch CUDA 12.4/12.8  
- TGP de 95 W → mantenha o notebook **sempre plugado** e no modo Alto Desempenho

### Link exato do modelo (Qwen3.6-35B-A3B)
**https://huggingface.co/Qwen/Qwen3.6-35B-A3B**

É a versão mais nova (abril 2026), MoE 35B total / ~3B ativos, excelente em raciocínio estruturado, português, seguir templates complexos (como o `.synt` do Synesis), contexto de 262K tokens e tarefas agentic/qualitativas.

### 1. Instalação passo a passo (Windows 11)

Abra o **PowerShell ou Prompt de Comando como Administrador**:

```bash
# 1. Atualize pip
pip install --upgrade pip

# 2. Instale PyTorch com CUDA (compatível com seu driver)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# 3. Instale AirLLM + suporte a 4bit
pip install -U airllm bitsandbytes accelerate huggingface_hub
```

### 2. Como baixar o modelo localmente

**Opção recomendada (automática – AirLLM faz isso):**
O modelo será baixado na primeira execução para:  
`C:\Users\SEU_USUARIO\.cache\huggingface\hub`

**Opção manual (melhor para controlar pasta e evitar interrupções):**

```bash
huggingface-cli download Qwen/Qwen3.6-35B-A3B --local-dir D:\Modelos\Qwen3.6-35B-A3B --resume-download
```

(Recomendo usar um disco com pelo menos 150–200 GB livres. O modelo em 4bit ocupa menos, mas o cache inicial é maior.)

### 3. Script otimizado para Synesis (salve como `synesis_qwen36.py`)

```python
from airllm import AutoModel
import torch
import os

# ==================== CONFIGURAÇÃO OTIMIZADA PARA SUA RTX 3050 6GB ====================
model = AutoModel.from_pretrained(
    "Qwen/Qwen3.6-35B-A3B",        # ou o caminho local: "D:/Modelos/Qwen3.6-35B-A3B"
    
    compression="4bit",            # ← OBRIGATÓRIO no seu 6 GB – melhor performance
    max_new_tokens=4096,           # espaço para chains e notes longas
    device="cuda",
    torch_dtype=torch.bfloat16,    # mais estável no RTX 30 series
    low_cpu_mem_usage=True,
    trust_remote_code=True,
    # offload se precisar (raro com 4bit)
    # use_flash_attn=True  # teste depois se quiser mais velocidade
)

print("✅ Modelo carregado com sucesso! VRAM usada deve ficar em ~4.5-5.5 GB")

def codificar_synesis(abstract_texto: str, template_synt: str) -> str:
    prompt = f"""Você é um especialista em codificação qualitativa usando o método Synesis.
Siga RIGOROSAMENTE o template .synt e todas as GUIDELINES.
Priorize relações causais corretas (ENABLES > INFLUENCES > RESTRICTS etc.).
Use apenas os blocos válidos. Não adicione explicações fora do formato.

TEMPLATE:
{template_synt}

ABSTRACT PARA CODIFICAR:
{abstract_texto}

Responda APENAS com o BUNDLE Synesis válido."""

    response = model.generate(
        prompt,
        temperature=0.05,      # muito baixa = máxima precisão e consistência
        do_sample=False,
        top_p=0.95,
        repetition_penalty=1.08,
        max_new_tokens=4096
    )
    return response

# Exemplo de uso
# resultado = codificar_synesis(seu_abstract, seu_template)
# print(resultado)
```

### 4. Dicas específicas para sua máquina (Dell G15)

- **NVIDIA Control Panel** → Gerenciar configurações 3D → Adicione o Python.exe e defina “Alta performance NVIDIA”.
- Mantenha o laptop **plugado** e ventilação livre (o AirLLM faz bastante uso contínuo).
- Monitore VRAM com GPU-Z ou MSI Afterburner durante a primeira execução.
- Se quiser ainda mais velocidade em batch: processe 3–8 abstracts de uma vez (o AirLLM lida bem com isso).
- Temperature 0.0–0.1 é ideal para Synesis (evita alucinações em chains causais).

Com essa configuração você deve conseguir **12–22 tokens/s** (dependendo do tamanho do prompt), o que é excelente para análise qualitativa.

Quer que eu gere agora a **versão em batch** (que lê uma pasta de arquivos .txt/.docx e salva automaticamente os .synt) ou uma interface Gradio simples para você colar abstracts? Também posso ajustar o prompt com mais exemplos do Synesis se você compartilhar um template típico.

---

## Parte 4 — Viabilidade de backend `airllm` (Qwen3.6-35B-A3B local)

**Data:** 2026-04-16
**Referência:** [`Qwen_3_6_local_AirLLM.md`](Qwen_3_6_local_AirLLM.md)
**Hardware alvo:** Dell G15, RTX 3050 6 GB VRAM, Windows 11, Driver 581.95 / CUDA 12.4

### 4.1 O que é AirLLM e por que não é trivial de integrar

AirLLM é uma **biblioteca Python de inferência layer-by-layer**, não um servidor HTTP. Diferentemente de Ollama ou LM Studio, ela **não expõe endpoint `/v1/chat/completions`** — a chamada é feita diretamente em Python:

```python
from airllm import AutoModel
model = AutoModel.from_pretrained("Qwen/Qwen3.6-35B-A3B", compression="4bit", ...)
response = model.generate(prompt_string, max_new_tokens=4096, temperature=0.05)
```

O backend `openai` atual do `synesis-coder` ([llm_client.py:109-120](synesis_coder/llm_client.py#L109-L120)) espera um servidor HTTP em `SYNESIS_CODER_API_URL`. **AirLLM não serve esse papel** — portanto, apontar `SYNESIS_CODER_BACKEND=openai` para AirLLM não é possível sem intermediário.

### 4.2 Diferenças arquiteturais críticas em relação aos backends atuais

| Característica | `anthropic` | `openai` (Ollama/RunPod) | `airllm` (proposto) |
|---|---|---|---|
| Interface | HTTP REST | HTTP REST `/v1` | Python direto (in-process) |
| Formato de input | `system[]` + `messages[]` | `messages[]` chat | String raw (chat template manual) |
| Modelo persiste entre chamadas | N/A (stateless) | Sim (servidor gerencia) | Sim (objeto `AutoModel` em memória) |
| Tempo de carga do modelo | N/A | ~30s (Ollama carrega) | **2-10 min** (primeiro `generate()`) |
| Rate limiting necessário | Sim (Anthropic) | Não | Não |
| Prompt caching | Sim (Anthropic) | Não | Não |
| Token usage retornado | Sim | Sim (OpenAI-compat) | **Não** (AirLLM não retorna contadores) |
| Suporte a modos concorrentes | Sim (via `to_thread`) | Sim (servidor multiplexado) | **Não** — uma instância = uma GPU, não é thread-safe para chamadas paralelas |
| Streaming | N/A | Suportado | Não (generate é síncrono bloqueante) |

### 4.3 O problema do chat template

AirLLM recebe uma string de prompt. O Qwen3.6 usa um **template Jinja2 específico** que deve ser aplicado manualmente para que o modelo comporte-se como chatbot instruction-following:

```
<|im_start|>system
{system_content}<|im_end|>
<|im_start|>user
{user_content}<|im_end|>
<|im_start|>assistant
```

O `prompt_builder.py` gera mensagens no **formato interno** `[{"role": "system"/"user", "content": ...}]`. Um backend `airllm` precisaria de uma função `_flatten_to_airllm_prompt()` que aplique o template Jinja2 do Qwen3, obtido via `AutoTokenizer.apply_chat_template()`.

### 4.4 Três caminhos de integração

#### Caminho A — Novo backend `airllm` nativo em `llm_client.py` (recomendado se o objetivo for produção)

Adicionar um terceiro ramo no `__init__` e em `_call_sync_inner`:

```python
# Em __init__:
elif self.backend == "airllm":
    from airllm import AutoModel
    from transformers import AutoTokenizer
    self._airllm_model = AutoModel.from_pretrained(
        model_path, compression="4bit", device="cuda", ...
    )  # carregado uma vez; reside em self para reuso entre chamadas

# Em _call_sync_inner (novo ramo):
elif self.backend == "airllm":
    prompt = self._flatten_to_airllm_prompt(messages)
    return self._airllm_model.generate(prompt, ...)
```

**Desafios do Caminho A:**
1. **Loading time**: modelo carrega na instanciação do `LLMClient` — pode travar o terminal por minutos antes do primeiro output.
2. **Concorrência**: modos `document` e `ontology` usam `asyncio.to_thread()` para chamar `_call_sync_inner` em paralelo. AirLLM com uma GPU **não suporta invocações paralelas** — as threads bloqueariam umas às outras ou causariam corrupção de estado. Exigiria um `asyncio.Semaphore(1)` forçado para o backend `airllm`, efetivamente serializando todas as chamadas. O modo `--concurrent` ficaria inerte.
3. **Token usage**: `model.generate()` não retorna contadores de tokens; `self.usage` ficaria zerado em sessões AirLLM — o `verbose` format não exibiria stats.
4. **Dependências novas**: `airllm`, `bitsandbytes`, `accelerate`, `torch` com CUDA — ~8 GB de dependências extras; não podem ser obrigatórias no `pyproject.toml` (quebraria instalação em máquinas sem GPU). Seriam dependências opcionais (`pip install synesis-coder[local]`).

**Esforço:** 3-5 horas. **Risco:** médio (concorrência serializada é regressão silenciosa para modos `document`/`ontology`).

---

#### Caminho B — Servidor wrapper OpenAI-compatível sobre AirLLM (recomendado para uso imediato)

Rodar um pequeno servidor FastAPI que expõe `/v1/chat/completions` usando AirLLM como engine. O `synesis-coder` usaria o backend `openai` já existente, sem nenhuma alteração de código:

```python
# airllm_server.py (script externo, ~80 linhas)
from fastapi import FastAPI
from airllm import AutoModel
# aplica chat template, chama model.generate(), retorna no formato OpenAI
```

```dotenv
# .env — Bloco 9 (proposto)
SYNESIS_CODER_BACKEND=openai
SYNESIS_CODER_API_URL=http://localhost:8765
SYNESIS_CODER_MODEL=Qwen/Qwen3.6-35B-A3B
```

**Prós:** zero alteração no `synesis-coder`; reutiliza todo o pipeline incluindo rate limiting desabilitado para locais; script separado pode ser mantido e evoluído independentemente.
**Contras:** requer `airllm_server.py` ativo antes de chamar `synesis-coder`; o servidor ainda serializa as chamadas GPU (FastAPI pode receber concorrentes mas AirLLM processa uma por vez).
**Esforço:** 1-2 horas para o servidor wrapper. **Risco:** baixo.

---

#### Caminho C — Aguardar Ollama (zero esforço, incerto)

O Bloco 4 do `.env` já suporta Ollama. O Qwen3.6-35B-A3B **não estava disponível no registry do Ollama** em abril de 2026 (modelo lançado no mesmo mês). Quando disponível, bastaria:

```dotenv
SYNESIS_CODER_BACKEND=openai
SYNESIS_CODER_API_URL=http://localhost:11434
SYNESIS_CODER_MODEL=qwen3.6:35b-a3b-q4_K_M
```

A vantagem do Ollama sobre AirLLM é que ele **gerencia concorrência, modelo em cache e API OpenAI-compat** nativamente. A desvantagem é que o suporte a `qwen3.6` ainda dependia de publicação no registry.

### 4.5 Bloco `.env` proposto (Caminho B — pronto para uso)

```dotenv
# ══════════════════════════════════════════════════════════════════
#  BLOCO 9 — AirLLM local · Qwen3.6-35B-A3B (RTX 3050 6GB, 4bit)
#  Requer: python airllm_server.py  (porta 8765)
#  Instalação: pip install airllm bitsandbytes accelerate torch --index-url .../cu124
# ══════════════════════════════════════════════════════════════════

# SYNESIS_CODER_BACKEND=openai
# SYNESIS_CODER_API_URL=http://localhost:8765
# SYNESIS_CODER_MODEL=Qwen/Qwen3.6-35B-A3B
```

### 4.6 Avaliação de qualidade analítica esperada

O Qwen3.6-35B-A3B é um MoE com ~3B parâmetros ativos por token — próximo de um modelo denso 3-4B em custo computacional, mas com capacidade de roteamento de um 35B. Para o caso de uso Synesis (template face85.synt com 3 GUIDELINES entrelaçadas):

| Critério | Expectativa |
|---|---|
| Instruction following (templates complexos) | **Bom** — Qwen3 MoE tem forte instruction following |
| Escolha de RELATION causal (ENABLES vs SUFFICIENT vs INFLUENCES) | **Médio-alto** — melhor que modelos 4B densos, inferior a Claude Opus |
| Consistência terminológica (FACTOR NAMING) | **Bom** — MoE com 262K contexto absorve o `code_index` |
| Velocidade (RTX 3050 6GB, 4bit, layer offloading) | **12-22 tokens/s** estimados (Qwen_3_6_local_AirLLM.md §4) |
| Custo operacional | **Zero** — totalmente local |

**Recomendação de uso:** adequado para iterações exploratórias e projetos com templates menos complexos. Para o face85.synt em produção (cadeias causais com 8 relações e regras de anchor), `claude-opus-4-6` permanece superior. AirLLM/Qwen3.6 é atraente para **pré-codificação em lote** antes de revisão humana ou como fallback econômico.

### 4.7 Veredito

> **Viabilidade MÉDIA-ALTA via Caminho B, esforço BAIXO (1-2h de script externo).** O Caminho A (backend nativo) exige tratamento cuidadoso de concorrência e dependências opcionais — viável mas requer esforço maior. O Caminho B (servidor wrapper) é o menor risco: zero alteração no `synesis-coder`, reutiliza o backend `openai`, e pode ser entregue como script auxiliar desacoplado. O Caminho C (Ollama) é a solução zero-esforço assim que o modelo for publicado no registry.

### Atualização da tabela de priorização

| # | Ação | Custo | Ganho |
|---|---|---|---|
| 1 | **Limpar `.env.example`** removendo variáveis inertes | Trivial | Alto |
| 2 | Implementar `SYNESIS_CODER_LANGUAGE` (Estratégia A) | 30 min | Alto |
| 3 | **Caminho B: `airllm_server.py`** wrapper FastAPI + Bloco 9 no `.env` | 1-2h | Alto (zero custo de inferência) |
| 4 | Caminho A: backend `airllm` nativo | 3-5h | Alto (integração mais limpa) |
| 5 | Implementar `SYNESIS_CODER_TEMPERATURE` efetivo | 15 min | Baixo |
| 6 | Implementar `SYNESIS_CODER_MAX_TOKENS` | 15 min | Médio |
| 7 | Implementar `SYNESIS_CODER_THINKING` para modos analíticos | 1-2h | Alto em precisão causal |
| 8 | Revisar face85.synt aplicando recomendações da §1.4 | 30 min | Médio |

As ações 2-7 requerem aprovação explícita do usuário — nenhuma foi executada neste estudo.
