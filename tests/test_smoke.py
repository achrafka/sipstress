"""End-to-end smoke tests using a local mock SIP UAS.

Run with: python -m unittest tests.test_smoke
"""
from __future__ import annotations

import asyncio
import os
import unittest

from sipstress.engine.call import DirectorTarget, IvrStep
from sipstress.engine.runner import Runner, RunnerConfig
from sipstress.analysis.health import analyze_run, HealthThresholds
from sipstress.plan import spec as plan_spec

from .mock_sip_server import start_server


class SmokeTest(unittest.TestCase):
    def test_options_against_mock(self) -> None:
        async def run() -> None:
            transport, proto, addr = await start_server("127.0.0.1", 0)
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="options",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:probe@{host}",
                    total_calls=20,
                    cps=200,
                    concurrency=10,
                    bind_ip="127.0.0.1",
                    bind_port=0,
                )
                runner = Runner(cfg)
                await runner.run()
                metrics_dicts = [m.to_dict() for m in runner.metrics.values()]
                health = analyze_run(metrics_dicts, HealthThresholds())
                self.assertEqual(metrics_dicts[0]["calls"]["attempted"], 20)
                self.assertEqual(metrics_dicts[0]["calls"]["succeeded"], 20)
                self.assertEqual(health["overall_verdict"], "OK")
                self.assertEqual(proto.options, 20)
            finally:
                transport.close()

        asyncio.run(run())

    def test_invite_against_mock(self) -> None:
        async def run() -> None:
            transport, proto, addr = await start_server("127.0.0.1", 0)
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="invite",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:callee@{host}",
                    total_calls=10,
                    cps=50,
                    concurrency=10,
                    call_duration_s=0.1,
                    bind_ip="127.0.0.1",
                    bind_port=0,
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 10)
                self.assertEqual(m["calls"]["succeeded"], 10)
                self.assertEqual(proto.invites, 10)
                self.assertEqual(proto.byes, 10)
                self.assertEqual(proto.acks, 10)
            finally:
                transport.close()

        asyncio.run(run())

    def test_invite_media_early_media_before_200(self) -> None:
        """183 with SDP starts RTP immediately; 200 arrives later → same session."""

        async def run() -> None:
            transport, proto, addr = await start_server(
                "127.0.0.1", 0, early_media=True, answer_after_s=0.2
            )
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="invite_media",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:pstn@{host}",
                    total_calls=1,
                    cps=1,
                    concurrency=1,
                    call_duration_s=2.0,
                    max_call_duration_s=45.0,
                    media_enabled=True,
                    rtp_port_min=44200,
                    rtp_port_max=44220,
                    bind_ip="127.0.0.1",
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 1, m)
                self.assertEqual(m["calls"]["succeeded"], 1, m)
                rec = m["call_records"][0]
                self.assertTrue(
                    rec.get("early_media_rtp"),
                    f"expected early RTP; events={rec.get('events')}",
                )
                self.assertEqual(proto.invites, 1)
                self.assertEqual(proto.byes, 1)
            finally:
                transport.close()

        asyncio.run(run())

    def test_ivr_against_mock(self) -> None:
        async def run() -> None:
            transport, proto, addr = await start_server("127.0.0.1", 0)
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="ivr",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:5555551001@{host}",
                    total_calls=1,
                    cps=1,
                    concurrency=1,
                    call_duration_s=0.0,
                    max_call_duration_s=10.0,
                    media_enabled=True,
                    rtp_port_min=41100,
                    rtp_port_max=41200,
                    bind_ip="127.0.0.1",
                    detail_log=True,
                    ivr_plan=[
                        IvrStep(wait_s=0.2),
                        IvrStep(digits="1"),
                        IvrStep(wait_s=0.3),
                        IvrStep(digits="5555551001"),
                        IvrStep(wait_s=0.3),
                        IvrStep(digits="#"),
                    ],
                    ivr_post_play_s=0.3,
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 1)
                self.assertEqual(m["calls"]["succeeded"], 1)
                # Must have at least one call record with full IVR detail.
                records = m.get("call_records") or []
                self.assertEqual(len(records), 1)
                ivr = records[0].get("ivr") or {}
                self.assertTrue(ivr.get("plan_completed"))
                # 3 digit-strings = 3 grouped DTMF entries (1 + 10 + 1) =
                # one entry per digit sent.
                sent = ivr.get("dtmf_sent") or []
                self.assertEqual(
                    "".join(d["digit"] for d in sent),
                    "1" + "5555551001" + "#",
                )
                # Audit log present
                self.assertIn("audit", records[0])
                # Mock saw a complete dialog
                self.assertEqual(proto.invites, 1)
                self.assertEqual(proto.acks, 1)
                self.assertEqual(proto.byes, 1)
            finally:
                transport.close()

        asyncio.run(run())


    def test_ivr_early_media_then_cancel(self) -> None:
        """IVR plays prompt over 183 and never sends 200 -> CANCEL after plan."""

        async def run() -> None:
            transport, proto, addr = await start_server(
                "127.0.0.1", 0, early_media=True, answer_after_s=None
            )
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="ivr",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:5555551001@{host}",
                    total_calls=1,
                    cps=1,
                    concurrency=1,
                    max_call_duration_s=10.0,
                    media_enabled=True,
                    rtp_port_min=41300,
                    rtp_port_max=41400,
                    bind_ip="127.0.0.1",
                    detail_log=True,
                    ivr_plan=[
                        IvrStep(wait_s=0.1),
                        IvrStep(digits="1"),
                        IvrStep(wait_s=0.2),
                        IvrStep(digits="5555551001"),
                    ],
                    ivr_post_play_s=0.1,
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 1)
                self.assertEqual(m["calls"]["succeeded"], 1)
                rec = m["call_records"][0]
                self.assertTrue(rec["ivr"]["plan_completed"])
                # Was an early-media + cancel run
                event_kinds = {e["kind"] for e in rec.get("events", [])}
                self.assertIn("early_media", event_kinds)
                self.assertIn("cancel_sent", event_kinds)
                self.assertIn("ivr_cancelled_after_completion", event_kinds)
                self.assertEqual(proto.cancels, 1)
                self.assertEqual(proto.byes, 0)
            finally:
                transport.close()

        asyncio.run(run())

    def test_ivr_early_media_then_answer(self) -> None:
        """IVR sends 183 first, then 200 mid-plan -> ACK + BYE."""

        async def run() -> None:
            transport, proto, addr = await start_server(
                "127.0.0.1", 0, early_media=True, answer_after_s=0.4
            )
            try:
                host, port = addr[0], addr[1]
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="ivr",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:5555551001@{host}",
                    total_calls=1,
                    cps=1,
                    concurrency=1,
                    max_call_duration_s=10.0,
                    media_enabled=True,
                    rtp_port_min=41400,
                    rtp_port_max=41500,
                    bind_ip="127.0.0.1",
                    detail_log=True,
                    ivr_plan=[
                        IvrStep(wait_s=0.1),
                        IvrStep(digits="1"),
                        IvrStep(wait_s=0.6),
                        IvrStep(digits="2"),
                    ],
                    ivr_post_play_s=0.1,
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 1)
                self.assertEqual(m["calls"]["succeeded"], 1)
                rec = m["call_records"][0]
                self.assertTrue(rec["ivr"]["plan_completed"])
                self.assertEqual(proto.cancels, 0)
                self.assertEqual(proto.byes, 1)
            finally:
                transport.close()

        asyncio.run(run())


    def test_plan_against_mock_with_early_media(self) -> None:
        """End-to-end TestPlan execution under early-media + late 200."""

        async def run() -> None:
            transport, proto, addr = await start_server(
                "127.0.0.1", 0, early_media=True, answer_after_s=1.0
            )
            try:
                host, port = addr[0], addr[1]
                plan = plan_spec.TestPlan(
                    name="mini",
                    description="three-step plan",
                    steps=[
                        plan_spec.StepSpec(
                            id="hello", type=plan_spec.StepType.PLAY, name="welcome",
                            expect_prompt_within_s=2.0,
                            min_prompt_duration_s=0.0,
                            max_duration_s=0.6,
                            expect_audible=False,  # mock has no real audio
                        ),
                        plan_spec.StepSpec(
                            id="lang", type=plan_spec.StepType.MENU, name="lang",
                            expect_audible=False,
                            valid_digits=["1", "2"],
                            send_digit="1",
                            max_duration_s=0.8,
                        ),
                        plan_spec.StepSpec(
                            id="cid", type=plan_spec.StepType.GET_DIGITS, name="cid",
                            expect_audible=False,
                            send_digits="12345", terminator="#",
                            max_duration_s=2.0,
                        ),
                    ],
                )
                cfg = RunnerConfig(
                    directors=[DirectorTarget.from_uri("mock", f"sip:{host}:{port}")],
                    scenario="ivr",
                    from_uri=f"sip:tester@{host}",
                    to_uri=f"sip:5555551001@{host}",
                    total_calls=1, cps=1, concurrency=1,
                    max_call_duration_s=15.0,
                    media_enabled=True,
                    rtp_port_min=41500, rtp_port_max=41600,
                    bind_ip="127.0.0.1",
                    detail_log=True,
                    test_plan=plan,
                )
                runner = Runner(cfg)
                await runner.run()
                m = next(iter(runner.metrics.values())).to_dict()
                self.assertEqual(m["calls"]["attempted"], 1)
                self.assertEqual(m["calls"]["succeeded"], 1)
                rec = m["call_records"][0]
                p = rec.get("plan")
                self.assertIsNotNone(p, "plan key missing in call record")
                self.assertEqual(len(p["steps"]), 3)
                self.assertTrue(p["completed"])
                # Last step (get_digits) must show the DTMF we sent.
                last = p["steps"][-1]
                self.assertEqual(last["dtmf"]["sent"], list("12345#"))
            finally:
                transport.close()

        asyncio.run(run())

    def test_pv3_studio_json_walker(self) -> None:
        """A minimal Studio JSON is walked into a TestPlan."""
        import json
        import tempfile

        from sipstress.plan import load_pv3_studio_json

        scenario = {
            "name": "demo",
            "containers": [
                {"uuid": "u-start", "type": "Start", "name": "Start",
                 "parameters": {}},
                {"uuid": "u-play", "type": "Play", "name": "Welcome",
                 "parameters": {}},
                {"uuid": "u-menu", "type": "Menu", "name": "MainMenu",
                 "parameters": {}},
                {"uuid": "u-dial", "type": "DialSimple", "name": "ToAgent",
                 "parameters": {}},
                {"uuid": "u-hang", "type": "Hangup", "name": "Bye",
                 "parameters": {}},
            ],
            "end-points": [
                {"source_uuid": "u-start", "target_uuid": "u-play", "value": ""},
                {"source_uuid": "u-play", "target_uuid": "u-menu", "value": ""},
                {"source_uuid": "u-menu", "target_uuid": "u-dial", "value": "1"},
                {"source_uuid": "u-menu", "target_uuid": "u-hang", "value": "2"},
                {"source_uuid": "u-dial", "target_uuid": "u-hang", "value": "default"},
            ],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(scenario, f)
            path = f.name
        try:
            plan = load_pv3_studio_json(path, branches_in_order=["1"])
            kinds = [s.type.value for s in plan.steps]
            self.assertEqual(kinds, ["note", "play", "menu", "dial", "hangup"])
            menu = [s for s in plan.steps if s.type.value == "menu"][0]
            self.assertEqual(sorted(menu.valid_digits), ["1", "2"])
            self.assertEqual(menu.branch_taken, "1")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
