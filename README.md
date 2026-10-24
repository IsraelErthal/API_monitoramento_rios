# Monitoramento de rios: ingestão e API

Recebe leituras do ESP32 receptor por HTTPS, valida, grava e entrega ao dashboard.
Em desenvolvimento usa SQLite (`dados.db`), sem Docker. No servidor, troque `DATABASE_URL` por PostgreSQL.

## Rodar no Windows (PowerShell)

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements-dev.txt
    Copy-Item .env.example .env        # edite e troque os dois tokens
    pytest                              # 62 testes devem passar
    uvicorn app.main:app --reload --env-file .env

Docs interativas: http://127.0.0.1:8000/docs. O `dados.db` vazio que você criou serve: as tabelas
e a estação `rio-piloto-01` (cotas de exemplo!) são criadas na primeira execução.

## Testar

    python simulator/send.py --url http://127.0.0.1:8000/api/v1/readings --token SEU_INGEST_TOKEN --event --errors

Ou pelo Postman: `POST /api/v1/readings` com `Authorization: Bearer <INGEST_TOKEN>` e o JSON do contrato.

## Contrato

Entrada (`POST /api/v1/readings`): `station_id`, `timestamp` (UTC, com fuso), `sequence`, `water_level_m`.

| Resposta | Significado | O receptor deve |
|---|---|---|
| 201 `accepted` | leitura nova gravada | seguir |
| 200 `duplicate` | já existia | não reenviar |
| 422 | inválida (campo, faixa, estação, futuro) | não reenviar |
| 401 | token errado | corrigir o token |
| 5xx / timeout | erro do servidor | reenviar depois (é idempotente) |

Duplicata: mesma `sequence` na mesma estação dentro de ±`DUP_WINDOW_MIN` (60 min), ou mesmo `timestamp`.
Se o contador do ESP reiniciar, as leituras novas passam depois dessa janela.

Saída (token `API_TOKEN`): `GET /api/v1/stations`, `/stations/{id}/latest`,
`/stations/{id}/history?from=&to=&limit=` (datas em UTC com `Z`), `/health`.

## Do SQLite ao servidor

- Servidor: `DATABASE_URL=postgresql+psycopg://usuario:senha@host:5432/rios` e `pip install "psycopg[binary]"`.
- Hoje a chave primária de `readings` é `id`. Para virar hipertabela do TimescaleDB, a chave precisa incluir
  o `timestamp`; com uma estação isso não é necessário, e a migração pode ser feita quando o volume pedir.
- Para evoluir as tabelas sem perder dados, adote o Alembic antes de mexer no esquema.
- No servidor, coloque um proxy com HTTPS (Caddy) na frente e não exponha o banco.

## Próximos passos

1. `processed_readings`: variações em 5, 15, 60 min e 3 h, média móvel, taxa de subida (m/h), sensor travado.
2. Regras de risco com as cotas reais e `GET /stations/{id}/alerts`.
3. WebSocket `/api/v1/live`.
4. Coleta ANA/Open-Meteo e primeiro modelo.

## Coletor de chuva (Open-Meteo)

Coleta chuva por ponto da bacia (o local da estação e pontos a montante) e guarda para as regras e o modelo.
Dados: *Weather data by Open-Meteo.com* (CC BY 4.0, mostre a atribuição no dashboard). O uso gratuito é para
fins não comerciais; confirme os termos se o uso mudar.

    python -m app.weather add-point --station rio-piloto-01 --name "Ponto local" --lat -28.6775 --lon -49.3708
    python -m app.weather add-point --station rio-piloto-01 --name "Montante 1" --lat ... --lon ... --role upstream
    python -m app.weather list-points
    python -m app.weather collect                 # uma coleta (rodar a cada hora)
    python -m app.weather backfill --from 2023-01-01 --to 2025-12-31    # histórico para treino

- Troque as coordenadas de exemplo pelas do ponto real. Quem enche o rio é a chuva a montante, não só a do sensor.
- `weather_forecasts`: previsões, **sem sobrescrever**. Cada coleta fica guardada (`collected_at`), o que permite
  saber depois o que a previsão dizia em cada momento e avaliar o modelo sem trapaça.
- `weather_recent`: chuva já ocorrida segundo o modelo meteorológico (não é pluviômetro). `source` separa as horas
  recentes (API de previsão) do histórico (arquivo ERA5), e reexecutar não duplica.
- Um ponto com falha não impede os demais; o comando sai com código 1 se algo falhou. Pelo mesmo motivo, o sistema de
  alerta nunca deve depender desta coleta: se ela parar, as regras do sensor continuam.
- Agendar: Windows, Agendador de Tarefas (a cada hora, `python -m app.weather collect` na pasta do projeto);
  servidor, cron `0 * * * *`. Para treinar o modelo, prefira o histórico do arquivo e, na operação, a previsão:
  a chuva "como foi prevista" erra, e a "como ocorreu" não.

## Tratamento, risco e alertas

Cada leitura gravada é processada **depois** de responder ao receptor (`app/pipeline.py`); uma falha ali nunca
derruba a ingestão. A leitura bruta não é alterada: os cálculos vão para `processed_readings`.

- **Derivados:** variação em 5, 15, 60 min e 3 h, média móvel de 1 h, máximo de 3 h, taxa de subida em m/h
  (inclinação dos últimos 60 min, robusta a ruído) e tendência (`rising`/`steady`/`falling`).
- **Sinalizações (`flags`):** `gap` (intervalo grande sem leitura), `jump` (variação impossível) e `stuck` (mesmo
  valor por muito tempo; só avisa, pois um rio estável também fica parado).
- **Salto impossível:** a leitura suspeita não gera alarme nem some; o risco fica como estava. Se a leitura seguinte
  a confirmar, é aceita (cheia real); se voltar ao normal, era defeito.
- **Risco:** `normal`, `attention` (acima da cota de atenção **ou** subindo rápido), `alert`, `critical`, ou `unknown`
  se a estação não tem cotas. Regras determinísticas, sem IA, com histerese (0,05 m) para o risco não oscilar.
- **Alertas (`alerts`):** um aberto por estação e tipo (`risk`, `sensor_offline`, `sensor_stuck`). Escalar ou descer
  fecha o anterior e abre outro; normalizar fecha. Todo o histórico fica guardado, com a mensagem do motivo.
- **Watchdog:** `python -m app.watchdog` (agendar a cada minuto, ou `--loop 60`) abre `sensor_offline` quando não
  chegam leituras há `OFFLINE_MIN` (15). O alerta fecha sozinho quando o sensor volta.

Novas rotas (token do dashboard): `GET /api/v1/stations/{id}/status` (última leitura, derivados, risco, motivos,
idade do dado, `stale` e alertas abertos) e `GET /api/v1/stations/{id}/alerts?open_only=`.
Se `stale` for verdadeiro, o dashboard deve mostrar "sem dados recentes" e não um risco "normal".

**Todos os limiares são valores iniciais** (`max_rate_m_h`, `rise_attention_m_h`, `stuck_min`, etc. em
`app/config.py`, ajustáveis por variável de ambiente em maiúsculas). Calibre com dados reais do ponto e valide as
cotas com a Defesa Civil. As cotas da estação de exemplo (2,5 / 3,0 / 3,5 m) são placeholders.

Ainda não há canal ativo de aviso (push, e-mail, SMS): o alerta só aparece para quem abrir o dashboard.

## Tempo real (SSE)

`GET /api/v1/live` (token do dashboard) mantém uma conexão aberta e empurra eventos assim que acontecem:
`hello`, `reading` (leitura processada, com risco e tendência), `alert_opened`, `alert_closed` e `prediction`.
O stream só traz o que acontece **depois** de conectar: ao (re)conectar, busque o estado atual em `/status`.
Os eventos são pequenos; os detalhes vêm das rotas REST.

- Os eventos saem do banco (consulta a cada `SSE_POLL_S` = 2 s por conexão), então funcionam também para alertas
  abertos pelo watchdog e com vários processos do servidor.
- O `EventSource` do navegador **não envia cabeçalho de autorização**. O dashboard deve usar uma biblioteca baseada
  em `fetch` (ex.: `@microsoft/fetch-event-source`) ou ter o próprio servidor dele repassando o stream. Não ponha o
  token na URL: ela fica em logs.
- Atrás de proxy: o Caddy precisa de `reverse_proxy app:8000 { flush_interval -1 }` para não segurar os eventos.

## Modelo de previsão (modo sombra)

Prevê **quanto o nível vai variar em 1, 3 e 6 horas** (e daí o nível futuro). Resultado em `predictions` e em
`GET /api/v1/stations/{id}/predictions`. **Não alimenta risco nem alertas**: grava a previsão, e depois se compara
com o que de fato aconteceu. Só passa a influenciar alertas por uma decisão explícita, depois dessa comparação.

Instalação separada (bibliotecas pesadas, algumas centenas de MB): `pip install -r requirements-ml.txt`.
Sem elas a API funciona normalmente, só sem previsão.

### 1. Dados manuais

CSV com `timestamp`, `water_level_m` e, se houver, `rain_mm` (chuva da hora que **termina** naquele horário). Veja
`exemplo_dados_manuais.csv`. Horários sem fuso são lidos como `America/Sao_Paulo` (`--tz` muda).

- A resolução importa. Para prever em horas é preciso nível de hora em hora ou melhor (use `--step-min 60`).
  **Médias diárias não servem** para previsão de 1 a 6 h.
- O mínimo técnico do código é de cerca de 3 semanas contínuas (10 min) ou 2 meses (horário), mas isso só faz o
  treino rodar. Para o modelo aprender algo, o período precisa ter **várias subidas de verdade** (cheias).
- Buracos de até 60 min são interpolados; maiores ficam vazios, e nada é inventado.

### 2. Treinar e avaliar

    python -m app.ml.train --csv meus_dados.csv --tz America/Sao_Paulo --step-min 60
    python -m app.ml.train --db rio-piloto-01            # com as leituras do próprio sensor

A validação é **temporal** (treina no passado, valida e testa no futuro, com folga entre os períodos). Compara com:
persistência ("o nível futuro será o de agora"), extrapolação linear, regressão Ridge e LightGBM (ou o equivalente
do scikit-learn). O relatório mostra o ganho sobre a persistência, inclusive nos eventos de subida.
Um modelo só é **aprovado** se reduzir o erro da persistência em pelo menos 5%, não for pior nos eventos de subida e
tiver eventos suficientes no teste. Dados sintéticos nunca são aprovados.

### 3. Retreinar com os dados recebidos do ESP

O ciclo é: a API recebe e salva a leitura; o pipeline calcula a qualidade e o risco; e, se houver um modelo
**aprovado para aquela estação**, gera as previsões. Um modelo de um rio nunca é usado em outro.

Depois de acumular histórico contínuo e algumas cheias reais, faça o primeiro treino:

    python -m app.ml.train --db rio-piloto-01 --step-min 10

Para manter o modelo atualizado sem intervenção, agende uma vez por dia (ou mantenha em execução). O executor
só tenta novo treino depois de acumular leituras novas; se os dados ainda forem insuficientes ou o candidato não
for aprovado, o último modelo aprovado continua em operação.

    python -m app.ml.runner --station rio-piloto-01 --step-min 10 --min-new-readings 144
    python -m app.ml.runner --all --loop 86400

`144` corresponde a aproximadamente um dia com uma leitura a cada 10 minutos. Ajuste `--step-min` para a
frequência real de transmissão do ESP (por exemplo, 60 para uma leitura horária).

### 4. Rodar em modo sombra

Com um modelo aprovado em `models_store/` (`MODELS_DIR`), cada leitura nova gera previsões automaticamente. Também dá
para agendar: `python -m app.ml.predict --station rio-piloto-01`. Depois de semanas:

    python -m app.ml.evaluate --station rio-piloto-01

compara cada previsão com o nível medido no horário-alvo, ao lado da persistência.

### Teste do código sem dados reais

    python -m app.ml.synthetic --days 180 --out sintetico.csv
    python -m app.ml.train --csv sintetico.csv --tz UTC

Serve **só** para verificar que o código roda. O gerador cria um "rio" que obedece uma equação simples; o bom
resultado dele não diz nada sobre o rio real, e o modelo sai sempre como não aprovado.

### O que a versão 1 não faz

- Não usa a **previsão** de chuva (só a chuva já ocorrida). Entra quando houver semanas de previsões guardadas pelo
  coletor, para treinar com a chuva "como foi prevista", e não como aconteceu.
- Não usa sazonalidade (mês do ano): com menos de um ano de dados ela só decoraria o período.
- Não usa estações a montante (ANA). O nível "médio" do rio entra como a feature `anomaly_7d` (nível atual menos a
  média dos últimos 7 dias).
- Um modelo treinado com a série de um ponto vale para aquele ponto. Se os dados manuais forem de outro local, o
  resultado serve para testar o processo, não para prever o rio do sensor.

## Operação mínima em campo

1. Configure o ESP/gateway para enviar um `POST /api/v1/readings` com o contrato acima e guardar localmente a
   leitura quando receber `5xx` ou timeout, reenviando depois com o mesmo `timestamp` e `sequence`.
2. Cadastre as cotas reais e a faixa física do sensor; as cotas da estação piloto não são utilizáveis em campo.
3. Agende `python -m app.watchdog` a cada minuto e `python -m app.weather collect` a cada hora.
4. Instale `requirements-ml.txt`, colete dados suficientes, execute o primeiro treino e mantenha o
   `app.ml.runner` agendado. A previsão permanece em modo sombra até a avaliação demonstrar ganho estável.
5. Antes de usar alertas para decisão, valide as regras e cotas com a Defesa Civil. Este software não substitui
   protocolos de emergência nem observação humana.
