from app.core.challenge import ChallengeAccount


class TestChallengeCreation:
    def test_phase1_defaults(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        assert c.initial_balance == 100_000.0
        assert c.current_balance == 100_000.0
        assert c.phase == "phase1"
        assert c.profit_target_pct == 8.0
        assert c.daily_drawdown_limit_pct == 5.0
        assert c.max_total_drawdown_pct == 10.0

    def test_phase2_defaults(self):
        c = ChallengeAccount.for_phase2(100_000.0)
        assert c.phase == "phase2"
        assert c.profit_target_pct == 5.0


class TestChallengeBalance:
    def test_apply_closed_trade_profit(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c2 = c.apply_closed_trade(500.0)
        assert c.current_balance == 100_000.0  # unchanged
        assert c2.current_balance == 100_500.0
        assert c2.equity_peak == 100_500.0

    def test_apply_closed_trade_loss(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c2 = c.apply_closed_trade(-300.0)
        assert c2.current_balance == 99_700.0
        assert c2.equity_peak == 100_000.0  # peak preserved

    def test_end_day_resets_pnl(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(200.0)
        c = c.apply_closed_trade(-100.0)
        assert sum(c.daily_pnl) == 100.0
        c2 = c.end_day()
        assert c2.daily_pnl == ()

    def test_immutability(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c2 = c.apply_closed_trade(500.0)
        c3 = c2.apply_closed_trade(200.0)
        assert c.current_balance == 100_000.0
        assert c2.current_balance == 100_500.0
        assert c3.current_balance == 100_700.0


class TestChallengeRules:
    def test_no_violations_at_start(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        assert not c.is_breached()
        assert c.rule_violations() == []

    def test_daily_drawdown_violation(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        # Simulate peak at 101_000 then drop to 95_500
        c = c.apply_closed_trade(1_000.0)     # peak = 101_000
        c = c.apply_closed_trade(-5_500.0)    # balance = 95_500, dd = 101_000-95_500 = 5_500 → 5.5%
        assert c.is_breached()
        assert any("Daily drawdown" in v for v in c.rule_violations())

    def test_total_drawdown_violation(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(-10_500.0)  # -10.5%
        assert c.is_breached()
        assert any("Total drawdown" in v for v in c.rule_violations())

    def test_no_violation_within_limits(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(500.0)
        c = c.apply_closed_trade(-400.0)      # -0.4%, no breach
        assert not c.is_breached()


class TestChallengeProgress:
    def test_progress_zero(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        assert c.progress_pct() == 0.0

    def test_progress_halfway(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(4_000.0)
        assert c.progress_pct() == 4.0

    def test_target_reached(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(8_000.0)
        assert c.is_target_reached()
        assert c.is_funded()

    def test_target_reached_but_breached(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(8_500.0)   # profit target met
        c = c.apply_closed_trade(-11_000.0)  # but total drawdown breached
        assert c.is_breached()


class TestChallengeSerialization:
    def test_to_dict(self):
        c = ChallengeAccount.for_phase1(100_000.0)
        c = c.apply_closed_trade(500.0)
        d = c.to_dict()
        assert d["initial_balance"] == 100_000.0
        assert d["current_balance"] == 100_500.0
        assert d["progress_pct"] == 0.5
        assert d["phase"] == "phase1"
        assert d["today_pnl"] == 500.0
