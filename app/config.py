import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Lê KEY=VALUE do .env (sem sobrescrever variáveis já definidas). Serve a API e os scripts."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variável de ambiente {name} não definida (veja .env.example)")
    return value


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    ingest_token: str          # usado pelo ESP32 receptor
    api_token: str             # usado pelo dashboard
    dup_window_min: int        # janela para considerar a mesma sequence como duplicata
    future_tolerance_s: int    # tolerância para relógios levemente adiantados


settings = Settings(
    database_url=os.environ.get("DATABASE_URL", "sqlite:///./dados.db"),
    ingest_token=_required("INGEST_TOKEN"),
    api_token=_required("API_TOKEN"),
    dup_window_min=int(os.environ.get("DUP_WINDOW_MIN", "60")),
    future_tolerance_s=int(os.environ.get("FUTURE_TOLERANCE_S", "300")),
)


@dataclass(frozen=True)
class Rules:
    """Parâmetros do tratamento e das regras de risco. TODOS são valores iniciais a calibrar
    com dados reais do ponto (podem ser alterados por variável de ambiente, em MAIÚSCULAS)."""
    max_rate_m_h: float = 2.0          # variação fisicamente impossível entre leituras
    confirm_tol_m: float = 0.3         # leitura suspeita é confirmada se a seguinte ficar a esta distância
    trend_m_h: float = 0.05            # abaixo disto o nível é considerado estável
    rise_attention_m_h: float = 0.3    # subida rápida que, sozinha, já é "atenção"
    hysteresis_m: float = 0.05         # para o risco descer, o nível precisa cair abaixo da cota por esta margem
    gap_min: int = 15                  # intervalo sem leitura que marca "gap"
    stuck_min: int = 180               # tempo com o mesmo valor que sugere sensor travado
    stuck_min_readings: int = 6
    stuck_eps_m: float = 0.001
    offline_min: int = 15              # sem leituras há este tempo = sensor offline


def load_rules() -> Rules:
    base = Rules()
    values = {}
    for name, default in base.__dict__.items():
        raw = os.environ.get(name.upper())
        values[name] = type(default)(raw) if raw else default
    return Rules(**values)


rules = load_rules()
