from __future__ import annotations

import unittest
from unittest import mock


class TestRecoveryModels(unittest.TestCase):
    def test_recoverable_billing_error(self):
        from harness.agent.recovery import is_model_recoverable_error

        exc = RuntimeError("INSUFFICIENT_BALANCE: account balance is not enough")
        self.assertTrue(is_model_recoverable_error(exc))

    def test_candidate_models_skip_current_and_missing_keys(self):
        from harness.agent.recovery import candidate_recovery_models

        catalog = [
            {"id": "a", "provider": "deepseek"},
            {"id": "b", "provider": "qwen"},
            {"id": "c", "provider": "zhipu"},
        ]

        class Provider:
            def __init__(self, pid):
                self.id = pid
                self.label = pid
                self.api_key_env = pid.upper() + "_KEY"
                self.api_key_fallback_env = None

        with mock.patch("harness.models.list_models", return_value=catalog), \
            mock.patch("harness.models.get_model", return_value="a"), \
            mock.patch("harness.models.get_model_profile", return_value=mock.Mock(provider="deepseek")), \
            mock.patch("harness.providers.config.get_provider", side_effect=lambda pid: Provider(pid)), \
            mock.patch("harness.providers.config.resolve_api_key", side_effect=lambda p: "k" if p.id == "qwen" else None), \
            mock.patch.dict("os.environ", {"HARNESS_RECOVERY_MODELS": "c,b"}, clear=False):
            self.assertEqual(candidate_recovery_models("a"), ["b"])


class TestClassicDisplay(unittest.TestCase):
    def test_failure_summary_contains_action(self):
        from harness.ui.classic_display import render_failure_summary

        text = render_failure_summary(
            user_query="做一下页面设计",
            errors=["PermissionDeniedError (HTTP 403): Insufficient account balance"],
            attempted_models=["deepseek-v4-pro", "glm-5.2-flash"],
            teammate_notes=["deepseek-pro failed: balance"],
        )
        self.assertIn("模型调用失败", text)
        self.assertIn("deepseek-v4-pro", text)
        self.assertIn("/model", text)

    def test_stats_dashboard_has_title(self):
        from harness.ui.classic_display import render_stats_dashboard

        text = render_stats_dashboard()
        self.assertIn("Usage Dashboard", text)
        # Regression: no hand-built Chinese dashboard label that previously misaligned.
        self.assertNotIn("用量仪表盘", text)


if __name__ == "__main__":
    unittest.main()
