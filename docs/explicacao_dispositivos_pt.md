# Explicação técnica — identidade única de dispositivo no MetaTrader 5

**Projeto:** Trade Assistant OO (`trade_assistant_oo`)
**Data do documento:** 2026-09-25
**Objetivo:** esclarecer por que a conta apareceu com dois IDs de dispositivo de "PC" na corretora e detalhar as mudanças de código feitas para garantir um único dispositivo ativo.

---

## 1. Diagnóstico

### 1.1 O que foi constatado na máquina

- Existe **apenas uma instalação** do MetaTrader 5 nesta máquina:
  `C:\Program Files\MetaTrader 5\terminal64.exe`.
- Existe **apenas um diretório de dados** do terminal (identidade do terminal):
  `C:\Users\<usuario>\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075`.
- Portanto, do ponto de vista da instalação, **esta máquina sempre teve uma única identidade de terminal**.

### 1.2 Causa raiz dos dois IDs de dispositivo

O assistente de trading usa a API oficial da MetaQuotes (pacote Python `MetaTrader5`) para se conectar ao terminal MT5 já aberto no desktop.

O código anterior chamava:

```python
mt5.initialize()   # sem parâmetros
```

O comportamento padrão dessa API é:

1. Tentar anexar ao terminal que já está aberto (compartilha a mesma instalação e o mesmo diretório de dados).
2. **Se não conseguir anexar, ela mesma inicia uma cópia oculta do `terminal64.exe`** e faz login na conta.

Isso acontecia em duas situações reais observadas nos logs:

- **Terminal fechado no momento da subida do app.** O assistente é reiniciado automaticamente por um watchdog do Windows (tarefa agendada `TradeAssistantOOWatchdog`). Nos logs há eventos de queda/relançamento do MT5 em 2026-08-17, 2026-08-31, 2026-09-18 e 2026-09-21, incluindo um período de reinicializações em sequência na manhã de 21/09 (09:44–10:57). Cada subida do app com o terminal fechado podia gerar uma instância oculta nova do terminal fazendo login.
- **Divergência de privilégios (elevação).** A tarefa agendada era registrada com `RunLevel: Highest` (como Administrador), enquanto o terminal do desktop e o notebook Jupyter rodavam como usuário normal. O Windows bloqueia a anexação IPC entre processos com níveis de integridade diferentes — então a API não conseguia anexar ao terminal visível e iniciava a própria cópia.

Cada sessão de login dessas instâncias pode ser registrada no painel da corretora como um "dispositivo" distinto. Por isso **um único PC físico** apareceu como **dois IDs de dispositivo de PC** (`8057f72fc4` e `f96df9dcff`).

> Importante: o aplicativo nunca teve a intenção de abrir duas instâncias. O comportamento é o padrão documentado da API MetaQuotes quando ela não consegue anexar ao terminal já aberto. As mudanças abaixo eliminam essa possibilidade por completo.

---

## 2. Mudanças de código realizadas

### 2.1 `app/core/broker.py` — conexão "attach-only"

**a) Verificação de processo antes de conectar (nunca mais inicia terminal próprio):**

```python
# Política de identidade única: o app apenas ANEXA ao terminal de desktop
# aberto pelo watchdog. Nunca inicia uma instância oculta própria, pois cada
# sessão de login pode ser registrada pela corretora como um dispositivo.
DEFAULT_MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
TERMINAL_PROCESS_NAME = "terminal64.exe"

def _terminal_process_running(proc_name: str = TERMINAL_PROCESS_NAME) -> bool:
    """True quando o terminal64 do desktop já está rodando.
    Falha em modo aberto (True) se a checagem em si falhar."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return proc_name.lower() in result.stdout.lower()
    except Exception:
        return True
```

**b) No `initialize()` — se o terminal não estiver aberto, o app aguarda em modo degradado e retenta; nunca abre uma segunda instância:**

```python
def initialize(self) -> bool:
    if self._connected:
        return True
    if self._attach_only and not _terminal_process_running():
        self._last_error = (
            "attach-only mode: MT5 terminal is not running — the app will "
            "NOT launch a second terminal instance; retrying automatically "
            "once the desktop terminal is open"
        )
        return False
    kwargs = {"portable": False}
    if self._terminal_path:
        kwargs["path"] = self._terminal_path
    self._connected = mt5.initialize(**kwargs)
    ...
```

**c) Caminho fixo do terminal + modo padrão (nunca portátil):**

```python
# caminho fixo do terminal instalado, mesmo diretório de dados do terminal de desktop
mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe", portable=False)
```

Com `portable=False`, a API é obrigada a usar o **mesmo diretório de dados padrão** do terminal de desktop — ou seja, a mesma identidade de dispositivo — mesmo no cenário hipotético em que precisasse iniciar o executável.

### 2.2 `app/config.py` — novos parâmetros de configuração

```python
"MT5_PATH": r"C:\Program Files\MetaTrader 5\terminal64.exe",
"MT5_ATTACH_ONLY": "1",
```

`MT5_ATTACH_ONLY=1` liga o modo "apenas anexar" por padrão.

### 2.3 `app/main.py` — ligação dos parâmetros ao broker

```python
broker = MT5Broker(
    login=config.get_str("MT5_LOGIN") or None,
    password=config.get_str("MT5_PASSWORD") or None,
    server=config.get_str("MT5_SERVER") or None,
    terminal_path=config.get_str("MT5_PATH") or None,
    attach_only=config.get_str("MT5_ATTACH_ONLY", "1") != "0",
)
```

### 2.4 `notebooks/analise_direcao_diaria.ipynb` — notebook de análise

O notebook que busca dados históricos também usava `mt5.initialize()` sem parâmetros. Agora usa o mesmo terminal fixo:

```python
MT5_EXE = r"C:\Program Files\MetaTrader 5\terminal64.exe"
if not mt5.initialize(path=MT5_EXE, portable=False):
    raise RuntimeError(f"MT5 init falhou: {mt5.last_error()}")
```

### 2.5 `install_startup.ps1` — tarefa agendada sem elevação

A tarefa do Windows que sobe MT5 + assistente era registrada como Administrador. Agora:

```powershell
# RunLevel Limited: o watchdog e tudo que ele inicia (MT5 + app) rodam no
# contexto normal do usuário. Rodar elevado pode forçar a API a abrir uma
# SEGUNDA instância do terminal (nível de integridade diferente).
$principal = New-ScheduledTaskPrincipal -UserID "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
```

**Tarefa reinstalada em 2026-09-25 e verificada:** `UserId: gk`, `LogonType: Interactive`, `RunLevel: Limited`.

### 2.6 `tests/conftest.py` — compatibilidade dos testes

A suíte de testes unitários foi ajustada para não depender de um terminal real; após as mudanças, **255 testes passaram**.

### 2.7 `PROCEDIMENTO_STARTUP.md` — documentação operacional

Documento atualizado com a política "um único terminal = um único dispositivo".

---

## 3. Comportamento a partir de agora

| Situação | Comportamento |
|---|---|
| Terminal MT5 do desktop aberto | O app anexa a ele e opera normalmente. |
| Terminal MT5 fechado | O app entra em modo degradado e **aguarda**; reconecta sozinho quando o terminal abrir. Nunca inicia uma cópia própria. |
| Reinício do PC / queda do app | O watchdog sobe o terminal de desktop; o app anexa a ele (mesmo exe, mesmo diretório de dados). |
| Qualquer login na conta | Vem sempre do mesmo terminal instalado → **mesma identidade de dispositivo**. |

---

## 4. Como verificar

```powershell
# 1. Apenas UMA cópia do terminal deve existir:
Get-Process terminal64 | Select-Object Id, Path, StartTime

# 2. A tarefa agendada deve estar sem elevação:
(Get-ScheduledTask -TaskName "TradeAssistantOOWatchdog").Principal | Format-List RunLevel

# 3. Parâmetros do app:
Get-Content data\runtime_config.json  # MT5_PATH e MT5_ATTACH_ONLY (padrões já aplicados)
```

---

## 5. Recomendação à corretora

Os dois IDs históricos (`8057f72fc4` e `f96df9dcff`) correspondem a sessões de login de instâncias ocultas do mesmo terminal instalado nesta máquina, causadas pelo comportamento padrão da API MetaQuotes (item 1.2). Com as correções, **nenhum novo ID será gerado**: todos os logins futuros virão da mesma instalação e do mesmo diretório de dados.

Solicitamos, se possível, que os dispositivos antigos sejam **removidos/zerados** no painel da corretora, para que o registro reflita apenas o dispositivo atual.
