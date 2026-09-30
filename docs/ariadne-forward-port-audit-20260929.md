# Auditoria Ariadne → Hermes upstream — 29/09/2026

## Decisão

**Não atualizar o pin nem fazer merge/rebase integral dos nove commits.** Implementar primeiro um patch aditivo de `session.status.usage`, usando o snapshot moderno do upstream. Esta branch é um candidato parcial de compatibilidade, não um runtime aprovado para a Ariadne.

Base exata: `NousResearch/hermes-agent@99721dca80a58830a7b04b954fa5cb58ccb51a4a`.
Fork auditado: `RaphaelAutomaton/hermes-agent@5cc116e2e0cdf3d5901a45dd7e2d1dc7e4cc372e`.
Ancestral comum: `9de9c25f620ff7f1ce0fd5457d596052d5159596` (release v0.18.2, julho).
GitHub confirmou `ahead_by=9`, `behind_by=31293`; isso conta commits alcançáveis, não 31.293 funcionalidades independentes.

O manifesto da Ariadne, lido pelo conector no snapshot de árvore `7c42b2b16448f4621f7d1b899d209c4fb6982d45`, ainda exige exatamente o fork acima. Seu escopo é o baseline Windows Workbench, Python 3.11; Linux/mobile exigem validação no destino. O upstream auditado prepara Python 3.14 e a validação desta branch usa 3.14.7. Não confundir a faixa permissiva de `requires-python` para atualização de instalações antigas com suporte de runtime atual.

Fontes: [comparação exata](https://github.com/NousResearch/hermes-agent/compare/99721dca80a58830a7b04b954fa5cb58ccb51a4a...RaphaelAutomaton:5cc116e2e0cdf3d5901a45dd7e2d1dc7e4cc372e), [pin Ariadne](https://github.com/RaphaelAutomaton/Ariadne/blob/main/docs/operations/runtime-compatibility.json), [bridge](https://github.com/RaphaelAutomaton/Ariadne/blob/main/tools/ariadne_hermes_runtime.py).

## Os nove patches e seu destino

“Absorvido” abaixo significa equivalência de mecanismo observada no código, não identidade de protocolo ou prova de implantação. Os diffs dos nove commits foram lidos; nenhum foi cherry-picked integralmente.

| Commit | Intenção | Situação no upstream auditado | Destino mínimo |
|---|---|---|---|
| `4cab2f1f1b162d00ade82c8a817d920b121cb7a1` | Aprovação correlacionada; fila UI; resume-lock/orphan-reaper | `tools/approval.py` já resolve pelo `request_id`; perguntas nativas usam `server_requests.py`; close/reaper retiram o registro sob lock e fazem teardown fora dele. O wire moderno mudou. | Não portar filas antigas do Desktop/TUI. Adaptar a bridge às perguntas nativas e provar correlação, cancelamento e reconexão. Reaproveitar o locking moderno. |
| `b795e93ca3eeaa985fded2e2b1ea48efbfa1b581` | SRM; cold read; cursor/snapshot; ledger; watch/unwatch; schema v20 | Há resume diferido/lazy, leases de execução, reaper/LRU, histórico persistente, replay de eventos e `/v1/runs` com idempotência. Não há os RPCs Ariadne `session.read`, `turn.submit/status`, `session.watch/unwatch` declarados no gateway TUI. | Dividir por contrato. Leitura cold e ledger têm valor; não restaurar o antigo `server.py` nem presumir que novos subsistemas são substitutos completos. Avaliar adapters sobre storage/runs antes de reinstalar managers. |
| `21b38678785048b6384b8ce7674af9937df083b4` | Provar hibernar/rematerializar; anunciar `runtime_pool=true` | Evicção moderna fecha/retira registros; não é a mesma máquina COLD/BUILDING/HOT_IDLE/EXECUTING/CLOSED com identidade conservada e limites de HOT_IDLE/executing. | Preservar o requisito se ainda desejado, mas não anunciar capability antes de provar a nova implementação. Portar a invariância e testes, não esse commit como unidade. |
| `ec5cb333c0c3aba047f08b39ede927696e368aee` | `session.status.usage` para heartbeat/medidor | `session.usage` e `_session_usage_snapshot` existem; `session.status` retorna só `output`. A bridge atual consome `usage` em status. | **Forward-port implementado nesta branch**: reutilizar snapshot moderno, contrato Pydantic e geração TS/OpenRPC. |
| `2485874788a43419d16c3205018590dee6b90c19` | Remover IDs estrangeiros no replay Codex | `_stored_message_item` já elimina ID não nativo no backend Codex; testes de adapter e transporte existem. Também há proteção de issuer/model para reasoning criptografado. | Não portar. O upstream usa prefixo `msg` e políticas adicionais de tamanho/reasoning, enquanto o patch antigo exigia `msg_`; manter a política atual e suas regressões. |
| `48646f660f3ea287e4f2c90f0b5c7cc9bbf66a1f` | Model/provider/reasoning/fast locais; persistência/restauração; opções nativas fast | `_create_overrides`, `model_switch.py`, `_set_reasoning`, `_set_fast`, runtime persistido e metadata mirror já cobrem grande parte. Fast usa resolução de modelo/provider/URL, não apenas um booleano. | Reutilizar upstream. Ainda validar opções/payloads que a UI Ariadne exige e informar níveis efetivamente enviados. Não recolocar writes globais ou `request_overrides` antigos. |
| `2b27a0b4ac00129fa2dda9dde32d83fc2cada7b3` | `skill.invoke` exclusivamente skills; catálogo dedicado; `session.queue` explícito | Há catálogo e `command.dispatch`; este pode resolver builtins/quick/plugin/exec. `prompt.submit(queued=true)` força queue se busy, mas permite começar turno quando idle/finish-race. | Garantias não absorvidas integralmente. Adaptar ou adicionar RPCs pequenos com contratos e testes: skill-only sem fallback e queue que rejeita idle sob lock. Não traduzir silenciosamente para dispatch/submit. |
| `5c7e02615b9db7d0b625e52f9cc19160a69f7372` | Incluir helper `approvalResponses.ts` e regressões omitidos | Corrige completude do patch antigo; UI nativa agora responde perguntas JSON-RPC correlacionadas. | Descartar o arquivo antigo no port. Conservar a invariância “remover apenas após confirmação da aprovação observada” no adapter Ariadne. |
| `5cc116e2e0cdf3d5901a45dd7e2d1dc7e4cc372e` | Lazy controls responsivos; reasoning display local/persistido; hidratação fenced | Status/interrupção têm caminhos modernos sem build; perguntas usam novo protocolo. `_set_reasoning` ainda escreve display sections do perfil para show/hide/full/clamp. `approval.respond` legado ainda usa `_sess`. | Port parcial necessário para **display por sessão** e bridge responsiva. Não assumir que isolamento de effort implica isolamento da exibição. A hidratação JS e a resposta legacy precisam de testes próprios. |

## Mudanças upstream de maior impacto

### Gateway e fronteira de segurança

* A antiga concentração em `tui_gateway/server.py` foi dividida entre `methods_*.py`, lifecycle, reaper, history, model switch, transport e compute host. Patches devem entrar no módulo temático; os métodos são rebinding para o namespace do facade via `method_ctx`, relevante também para monkeypatches.
* O wire agora é declarado em `tui_gateway/contracts`, valida parâmetros (campos extras proibidos por padrão), valida resultados em isolamento e gera TypeScript/OpenRPC. Novo campo ou RPC exige alterar o modelo e regenerar, além do handler.
* Perguntas approval/clarify/sudo/secret agora são **requisições JSON-RPC servidor→cliente**, com IDs `srq-<12 hex>`, resposta com o mesmo ID, `request.cancel`, `open_requests` e capability `client.capabilities {server_requests:true}`. A bridge Ariadne auditada espera notificações `approval.request` e IDs de 32 hex. É um bloqueio de integração, inclusive para sudo/secret, não só uma mudança de nome.
* Perfil deve bindar home **+ secrets + terminal policy**, inclusive teardown, background e subprocessos. Identidade autenticada WS é estampada pelo servidor, não por parâmetros fornecidos pelo cliente. Conservar ownership/transport checks da Ariadne; não dar autoridade a observers ou a uma sequência de replay.

### Sessões, persistência e recursos

* Lazy/deferred resume restaura runtime persistido e evita construir agente até necessário. `_session_usage_snapshot` respeita a autoridade do compute host, evitando reportar o agente local stale.
* Reaper tem TTL, cap LRU de sessões detached, exclusões para execução/build/input/subagentes, flush incremental e heartbeat cross-backend. Isso reduz a necessidade de partes do SRM, mas o cap é soft e não replica hibernação conservando o registro nem todos os limites do pool antigo.
* `session.events.since` usa ring limitado por eventos/bytes/processo, seq e epoch por processo. Truncamento manda refetch; reinício muda epoch. Não é armazenamento durável de eventos nem `session.watch` sem materialização.
* Storage está dividido em `hermes_state_*.py`, schema 31, leases de turnos por lineage e histórico/timeline com row IDs. As rotas HTTP de mensagens/timeline permitem leitura sem agente, mas cursor lógico de timeline não prova o contrato Ariadne de snapshot estável, byte limit, view dialog/timeline e isolamento por profile.
* `/v1/runs` já oferece reserva atômica `(scope,idempotency_key)` por fingerprint, status persistido, owner PID/start time, replay/conflito, eventos SSE e stop/steer/approval. É candidato melhor que duplicar infraestrutura. **Não é drop-in**: storage pode cair para memória e informa `durable=false`; há retenção/pruning; `update_status` não impõe terminal immutability nem generation-CAS no próprio store. Esses são gates para substituir `TurnLedger`.

### Reasoning e providers

* `agent/reasoning_effort.py` centraliza vocabulário e clamp por wire. UI deve distinguir effort escolhido de `reasoning_effort_wire`; por exemplo, `ultra` pode enviar `max`. Desativado deve permanecer diferente de unset.
* `reasoning_params.py` condiciona kwargs à rota/capacidade. Portar um mapa genérico antigo pode reintroduzir HTTP 400 em endpoints custom/OpenRouter e perda de parâmetros em Anthropic/native providers.
* Replay Codex agora protege issuer e modelo de conteúdo criptografado, IDs e pares de tools; compactação nativa é capability-gated. Aproveitar essas correções em vez de reinstalar o adapter antigo.
* Fast depende da rota real; overrides locais, provider custom persistido e respostas do compute host precisam sobreviver restore/rebuild. “Gateway Vidda” não é prova de suporte a priority nem de um nível de reasoning: validar requests reais do destino antes de habilitar opções.

### Ferramentas e implantação

* Discovery/registry/plugin/toolsets evoluíram. Capacidade de superfície deve vir da sessão, não de env do processo; cliente remoto não equivale a backend local. AIAgent é facade sobre módulos de turnos; providers que executam ferramentas projetam histórico append-only, sem reexecutar tool calls já concluídos.
* Skills/quick commands compartilham discovery. Um nome em catálogo não garante a resolução skill-only exigida pela Ariadne, especialmente diante de colisões e preprocessamento shell.
* PM prepara dependências/runtime com lock e grupos de teste; updater/bootstrap e Python 3.14 mudam o contrato operacional. O worker LiveKit isolado da Ariadne não deve ser atualizado junto por acidente.

## Implementação nesta branch

O patch preserva o texto de status e adiciona `usage` calculado uma vez pelo mecanismo upstream. Evita account/network fetch do RPC `session.usage`: o heartbeat não precisa consultar quota remota. Usa metadata mirror quando o compute host é a autoridade; conserva `{}` para sessão cold sem medida. Nunca usa lifetime tokens como ocupação de contexto.

Arquivos: handler em `tui_gateway/methods_session.py`, `SessionStatusResult.usage`, contratos TS/OpenRPC regenerados e `tests/tui_gateway/test_ariadne_status_usage.py`.
Inspiração funcional: `ec5cb333`; implementação adaptada à arquitetura moderna, sem restaurar o god-file.

Os testes novos passam pelo dispatcher real, validação do contrato e SQLite real em profile temporário; apenas construção de agente é interceptada para falhar se status tentar ativá-la. Cobrem agente local, compute host que supera agente stale, e cold sem inventar contexto. A falha RED no snapshot original foi `KeyError: usage` nos três casos.

As verificações e seus resultados finais estão em `docs/ariadne-forward-port-validation-20260929.json`. O runner usado foi o oficial `scripts/run_tests.sh`, com Python do ambiente PM construído a partir do lock, clean env e subprocesso por arquivo. Isso comprova este patch e os mecanismos listados, **não** os contratos end-to-end da Ariadne nem um upgrade de produção.

## Menor sequência completa ainda necessária

1. **Baseline e isolamento**: conservar o pin; registrar perfil, runtime e DB atuais. Backup consistente com WAL e ensaio de restauração. Usar cópias separadas de home/DB em cada runtime; não fazer upgrade in-place de um DB e depois prometer downgrade sem prova. Schema v20 do fork não é prova de compatibilidade com schema 31.
2. **Status aditivo**: este patch, contratos gerados e regressões. Não sinalizar `session_runtime` capabilities só porque status funciona.
3. **Adapter de perguntas** na Ariadne: mapear request/response/cancel/open_requests e IDs, mantendo allowlist, ownership, redaction e escolhas once/deny. Provar duas aprovações simultâneas, duplicatas, stale responses, reconexão, interrupt durante build/preprocess e secrets que não chegam ao browser/log.
4. **Controles**: conservar modelo/provider/reasoning/fast modernos; adaptar payload/capability consumidos pela UI. Portar display overrides por sessão sem writes em config de outra conversa. Provar A→B→A, cold restore, rebuild e responses de hidratação fora de ordem. Validar provider custom/Gateway Vidda com kwargs reais, sem inferir suporte.
5. **RPCs pequenos que preservam intenção**: `skill.invoke` skill-only e explicit queue com rejeição idle/finalização sob lock. Registrar contratos e testes de colisão skill vs builtin/quick/plugin, falha redigida, preprocessamento bloqueado com interrupt/approval disponível e finish race sem submit duplicado.
6. **Cold history**: adapter storage-only para `session.read`, mantendo perfil, lineage, view, cursor/snapshot/bytes. Provar zero agentes/clients/workers durante leitura, mudança concorrente do histórico, compressão entre páginas e cursor de outro profile recusado.
7. **Ledger/observação/pool**: primeiro escolher entre adaptar `/v1/runs`/event replay e preservar ledger Ariadne. Exigir durable storage fail-closed, duplicate vs conflict, writer generation fencing, recuperação com PID reutilizado, terminais imutáveis e observers sem ownership. Provar hibernation→submit na mesma conversa apenas se o pool continuar requisito; não copiar todo SRM por ausência do filename.
8. **Gate de pin**: rodar regressões da bridge/supervisor/security/UI da Ariadne contra o candidato, E2E WS real e DB migration/rollback em Windows baseline e Linux destino, mais ensaio LiveKit separado. Pin só muda em commit posterior com os recibos desses gates. Esta auditoria não executou essas validações e não autoriza declarar o port completo.

## Limites da auditoria

Análise concentrada nos nove diffs, no código upstream das áreas solicitadas e no consumidor atual da Ariadne. Não é revisão manual de todos os 31.293 commits nem auditoria exaustiva de segurança. Não houve chamada a LLM/provider pago, deploy na VPS, merge, alteração do pin, do perfil ou dos dados da Ariadne. Upstream foi congelado no SHA solicitado; esta conclusão não presume que `main` continue nesse SHA depois da auditoria.
