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
