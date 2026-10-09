# Procedimento de Startup — MT5 + Trade Assistant OO

## Como o sistema funciona

1. Tarefa agendada `TradeAssistantOOWatchdog` (AtLogOn, elevada) roda `start.ps1`.
2. O watchdog **nunca fecha o MT5** — ele apenas:
   - abre o MT5 pelo atalho do Menu Iniciar se o processo `terminal64` não estiver rodando;
   - aguarda ~60 s de "grace" para o auto-login do MT5;
   - inicia o app Flask (porta 8000) e monitora health a cada 15 s;
   - relança o MT5 se o processo morrer, **sem derrubar o app**;
   - se o health apontar `mt5_disconnected` com o terminal vivo, **não reinicia o app** (aguarda a reconexão).
3. O app tem **duas camadas de login**:
   - `mt5.login()` automático com Servidor/Conta/Senha salvos na página `/parm`
     (funciona quando o terminal está aberto, mesmo deslogado);
   - verificação pós-login via `account_info()` — só considera sucesso com a conta confirmada.
4. O MT5 em si também pode auto-logar, se o login foi salvo com a opção
   **"Keep me signed in"** marcada na tela de login.

## Um único terminal = um único dispositivo na corretora

- O app usa **attach-only**: ele só conecta ao `terminal64.exe` que já está
  aberto (o que o watchdog sobe pelo atalho do Menu Iniciar). Ele **nunca
  lança uma segunda instância escondida do MT5** — cada instância que loga na
  conta pode ser registrada pela corretora como um "dispositivo" diferente.
- O caminho do terminal é fixado em `MT5_PATH`
  (`C:\Program Files\MetaTrader 5\terminal64.exe`) e o modo portátil é
  desativado (`portable=False`), garantindo sempre o MESMO diretório de dados
  e a mesma identidade.
- Se o terminal não estiver aberto, o app aguarda em modo degradado e
  reconecta sozinho quando o MT5 subir (nada de instância escondida).
- A tarefa agendada roda **sem elevação** (`RunLevel Limited`): processo
  elevado + terminal normal pode quebrar o anexo IPC e gerar uma segunda
  instância do terminal.
- O notebook `notebooks/analise_direcao_diaria.ipynb` também fixa o mesmo
  `terminal64.exe` ao buscar dados do MT5.

> Se a corretora ainda mostrar dois dispositivos antigos, peça a ela para
> remover/zerar a lista de dispositivos do painel; a partir de agora todos os
> logins virão do mesmo terminal.

## Rotina semanal (reboot do PC)

1. Reinicie o Windows normalmente (desligamento limpo preserva o estado do MT5).
2. No logon, a tarefa sobe tudo sozinho — não é preciso abrir nada.
3. Verificação (2 min após o logon):
   - `http://localhost:8000/api/health` → `status: healthy` e `mt5_connected: true`;
   - `data\app.log` → sequência "MT5 started successfully" → "App healthy";
   - Telegram → mensagem de startup do app.

## Configuração única (recomendada)

- Ao logar no MT5, marque **"Keep me signed in"** (salvar senha) para o MT5
  se autenticar sozinho no boot.
- Mantenha Servidor/Conta/Senha atualizados em `http://localhost:8000/parm`
  (seção "Conexao MT5 (auto-login)") — é o fallback do app.
- Ao reinstalar a tarefa (`install_startup.ps1`, PowerShell **como administrador**),
  libere a política de execução só para a sessão caso dê
  "execução de scripts foi desabilitada":
  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
  .\install_startup.ps1
  ```
- A tarefa tem DOIS triggers: AtLogOn + repetição a cada 5 min (rede de segurança
  que sobe o watchdog se ele for encerrado; RestartCount do Agendador não se
  aplica a trigger de logon). `MultipleInstances=IgnoreNew` evita duplicatas.
- **Não feche a janela "Trade Assistant OO Watchdog"** — encerrar o watchdog
  derruba a supervisão (o trigger de 5 min re-suba em minutos, mas evite).

## Cenário "modo segurança" (MT5 volta deslogado)

Sintoma: MT5 abre na tela de login / sem perfil, e o log do terminal
(`%APPDATA%\MetaQuotes\Terminal\<hash>\logs\<data>.log`) contém
**"Accounts deleted due security reason"**.

Causa: o MT5 apaga as credenciais salvas quando o processo foi encerrado de
forma anormal (crash ou `Stop-Process -Force`). Nesse estado o canal IPC do
terminal pode ficar bloqueado e o auto-login do app não alcança o terminal.

Ação:
1. Na janela do MT5, feche qualquer aviso e faça o login manualmente
   (marque "Keep me signed in").
2. Não faça mais nada — o app reconecta sozinho em segundos
   (reconexão automática do broker + health checks).
3. Confirme em `/api/health` que voltou a `healthy`.

## Testes de resiliência (para quem for testar)

- **NUNCA** use `Stop-Process -Force` no `terminal64` para simular queda —
  isso corrompe o estado do MT5 (modo segurança) e exige login manual.
- Para testar o ciclo completo, use o reboot do Windows (fechamento limpo).
- Para testar o watchdog do app: `Stop-ScheduledTask -TaskName TradeAssistantOOWatchdog`
  e depois `Start-ScheduledTask -TaskName TradeAssistantOOWatchdog`
  (isso derruba também o MT5 que era filho da tarefa; na subida ele é relançado).
- Remover/instalar a tarefa: `.\install_startup.ps1 -Remove` / `.\install_startup.ps1`
  (PowerShell **como administrador**).

## Referências

- Watchdog: `start.ps1` | Instalador: `install_startup.ps1` (raiz do projeto)
- Log do watchdog: `data\app.log` | Log do app: `data\stdout.log`
- Health: `GET /api/health` | Estado: `GET /api/state`
- Auto-login do app: `app/core/broker.py` (`_ensure_login`)
