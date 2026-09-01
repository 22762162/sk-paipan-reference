"""Pure rule-scope resolution: precedence, binding and phase safety."""

import unittest

from aifos.rule_scope import (
    DuplicateRuleError,
    Rule,
    RuleBundle,
    RuleContext,
    RuleResolver,
    RuleScopeError,
    ScopeBindingError,
    resolve_rule_stack,
    resolve_rules,
)


PROJECT = "project-rain"
EPISODE = "episode-01"


def _context(**changes):
    values = {
        "project_id": PROJECT,
        "episode_id": EPISODE,
        "stage": "storyboard",
        "modality": "image",
        "shot_no": 7,
        "scene_no": 2,
        "story_phase": "present",
        "era": "民国",
    }
    values.update(changes)
    return RuleContext(**values)


class CreativePrecedenceTest(unittest.TestCase):
    def test_same_key_is_replaced_instead_of_concatenated(self):
        result = resolve_rules(
            _context(),
            system_base=[
                Rule("camera.motion", "static", source="system-v1"),
                Rule("frame.no_subtitles", True),
            ],
            project_series=RuleBundle(
                [Rule("camera.motion", "slow_push")],
                project_id=PROJECT,
                source="series-bible-v3",
            ),
            episode_temporary=RuleBundle(
                [Rule("camera.motion", "handheld")],
                project_id=PROJECT,
                episode_id=EPISODE,
                source="episode-patch-2",
            ),
            current_shot=RuleBundle(
                [Rule("camera.motion", "locked_closeup")],
                project_id=PROJECT,
                episode_id=EPISODE,
                source="shot-7-contract",
            ),
        )

        self.assertEqual(result["camera.motion"], "locked_closeup")
        self.assertIs(result["frame.no_subtitles"], True)
        self.assertNotIsInstance(result["camera.motion"], list)
        self.assertEqual(
            result.sources["camera.motion"]["layer"], "current_shot")
        self.assertEqual(
            [item["value"] for item in result.overridden],
            ["static", "slow_push", "handheld"],
        )

    def test_technical_hard_rule_wins_over_current_shot(self):
        result = resolve_rules(
            _context(),
            technical_hard=[Rule(
                "frame.no_subtitles", True, source="technical-gate")],
            current_shot=RuleBundle(
                [Rule("frame.no_subtitles", False, source="director-note")],
                project_id=PROJECT,
                episode_id=EPISODE,
            ),
        )

        self.assertIs(result["frame.no_subtitles"], True)
        self.assertTrue(
            result.sources["frame.no_subtitles"]["technical_hard"])
        self.assertEqual(
            result.overridden[-1]["reason"], "technical_hard_constraint")
        self.assertEqual(
            result.overridden[-1]["source"]["layer"], "current_shot")

    def test_rule_source_can_refine_bundle_source(self):
        result = resolve_rules(
            _context(),
            project_series=RuleBundle(
                [Rule("visual.era", "民国", source="era-bible")],
                project_id=PROJECT,
                source="project-bible",
            ),
        )
        self.assertEqual(result.sources["visual.era"]["source"], "era-bible")

    def test_episode_can_suppress_lower_creative_rule_only_for_this_episode(self):
        result = resolve_rules(
            _context(),
            system_base=[Rule("visual.default_ornament", "always")],
            episode_temporary={
                "project_id": PROJECT,
                "episode_id": EPISODE,
                "source": "episode-rule-v2",
                "rules": [],
                "suppressions": ["visual.default_ornament"],
            },
        )
        self.assertNotIn("visual.default_ornament", result.rules)
        self.assertEqual(
            result.overridden[-1]["reason"],
            "higher_creative_suppression")

    def test_technical_hard_rule_cannot_be_suppressed(self):
        with self.assertRaises(RuleScopeError):
            resolve_rules(
                _context(),
                technical_hard=[Rule("technical.resolution", "720p")],
                episode_temporary={
                    "project_id": PROJECT,
                    "episode_id": EPISODE,
                    "rules": [],
                    "suppressions": ["technical.resolution"],
                },
            )

    def test_project_suppression_cannot_hide_an_episode_binding(self):
        with self.assertRaisesRegex(
                ScopeBindingError, "cannot bind an episode_id"):
            resolve_rules(
                _context(),
                system_base=[Rule("visual.default", "base")],
                project_series={
                    "project_id": PROJECT,
                    "rules": [],
                    "suppressions": [{
                        "key": "visual.default",
                        "episode_id": EPISODE,
                    }],
                })


class ApplicabilityTest(unittest.TestCase):
    def test_all_supported_dimensions_must_match(self):
        exact = Rule(
            "render.look",
            "sepia",
            applicability={
                "stage": ["storyboard", "frames"],
                "modality": "image",
                "shot_no": [7, 8],
                "scene_no": "2",
                "story_phase": "present",
                "era": "民国",
            },
        )
        wrong_shot = Rule(
            "render.wrong_shot", True, applicability={"shot_no": 99})
        wrong_modality = Rule(
            "render.wrong_modality", True,
            applicability={"modality": "video"})

        result = resolve_rules(
            _context(), system_base=[exact, wrong_shot, wrong_modality])

        self.assertEqual(result.rules, {"render.look": "sepia"})
        self.assertEqual(
            result.sources["render.look"]["applicability"]["shot_no"],
            [7, 8],
        )

    def test_same_layer_variants_may_share_key_when_only_one_applies(self):
        result = resolve_rules(
            _context(modality="video"),
            system_base=[
                Rule("output.profile", "image", {"modality": "image"}),
                Rule("output.profile", "video", {"modality": "video"}),
            ],
        )
        self.assertEqual(result["output.profile"], "video")

    def test_two_simultaneously_applicable_same_layer_rules_are_rejected(self):
        with self.assertRaises(DuplicateRuleError):
            resolve_rules(
                _context(),
                system_base=[
                    Rule("camera.motion", "push"),
                    Rule("camera.motion", "pan"),
                ],
            )

    def test_unknown_applicability_dimension_is_rejected(self):
        with self.assertRaises(RuleScopeError):
            resolve_rules(
                _context(),
                system_base=[Rule(
                    "camera.motion", "push", {"location": "office"})],
            )

    def test_realm_phase_and_event_are_exact_selectors(self):
        scoped = Rule(
            "world.allowed_prop",
            "梦中怀表",
            applicability={
                "active_realm_ids": ["dream-hotel"],
                "active_story_phases": ["梦境"],
                "event_ids": ["event-7"],
            },
            exception_kind="dream",
        )
        matching = resolve_rules(
            _context(
                story_phase=None,
                active_story_phase="dream",
                active_realm_id="dream-hotel",
                event_id="event-7",
            ),
            system_base=[scoped],
        )
        another_event = resolve_rules(
            _context(
                story_phase=None,
                active_story_phase="dream",
                active_realm_id="dream-hotel",
                event_id="event-8",
            ),
            system_base=[scoped],
        )
        self.assertEqual(matching["world.allowed_prop"], "梦中怀表")
        self.assertNotIn("world.allowed_prop", another_event)
        self.assertEqual(
            matching.context.active_story_phase, matching.context.story_phase)

    def test_context_aliases_are_normalized_before_fingerprinting(self):
        aliases = resolve_rules(
            {
                "project_id": PROJECT,
                "episode_id": EPISODE,
                "active_story_phase": "梦境",
                "realm_id": "dream-hotel",
                "scene_event_id": "event-7",
                "era_context": "民国",
            },
            system_base=[Rule("visual.palette", "blue")],
        )
        canonical = resolve_rules(
            {
                "project_id": PROJECT,
                "episode_id": EPISODE,
                "story_phase": "dream",
                "active_realm_id": "dream-hotel",
                "event_id": "event-7",
                "era": "民国",
            },
            system_base=[Rule("visual.palette", "blue")],
        )
        self.assertEqual(aliases.fingerprint, canonical.fingerprint)

    def test_conflicting_story_phase_aliases_are_rejected(self):
        with self.assertRaisesRegex(
                RuleScopeError, "story_phase conflicts"):
            RuleContext(
                project_id=PROJECT,
                episode_id=EPISODE,
                story_phase="dream",
                active_story_phase="time_travel",
            )


class ContextBindingTest(unittest.TestCase):
    def test_project_rule_requires_and_matches_project_binding(self):
        with self.assertRaises(ScopeBindingError):
            resolve_rules(
                _context(), project_series=[Rule("visual.era", "modern")])

        with self.assertRaises(ScopeBindingError):
            resolve_rules(
                _context(),
                project_series=RuleBundle(
                    [Rule("visual.era", "future")],
                    project_id="another-project",
                ),
            )

    def test_episode_rule_cannot_leak_between_episodes(self):
        foreign = RuleBundle(
            [Rule(
                "wardrobe.hero", "black coat",
                applicability={"shot_no": 999})],
            project_id=PROJECT,
            episode_id="episode-02",
        )
        # Binding is checked before shot applicability: mixed episode storage is
        # rejected instead of silently appearing harmless today.
        with self.assertRaises(ScopeBindingError):
            resolve_rules(_context(), episode_temporary=foreign)

    def test_context_always_requires_project_and_episode(self):
        with self.assertRaises(RuleScopeError):
            resolve_rules({"project_id": PROJECT})


class StoryPhaseExceptionTest(unittest.TestCase):
    CASES = (
        ("time_travel", "穿越"),
        ("dream", "梦境"),
        ("play_within_play", "戏中戏"),
    )

    def test_exceptions_only_override_inside_their_phase(self):
        for exception_kind, phase in self.CASES:
            with self.subTest(exception_kind=exception_kind):
                exception_rule = Rule(
                    "world.era",
                    f"exception:{exception_kind}",
                    applicability={"story_phase": phase},
                    exception_kind=exception_kind,
                )
                bundle = RuleBundle(
                    [exception_rule],
                    project_id=PROJECT,
                    episode_id=EPISODE,
                )

                ordinary = resolve_rules(
                    _context(story_phase="present"),
                    system_base=[Rule("world.era", "民国")],
                    current_shot=bundle,
                )
                exceptional = resolve_rules(
                    _context(story_phase=phase),
                    system_base=[Rule("world.era", "民国")],
                    current_shot=bundle,
                )

                self.assertEqual(ordinary["world.era"], "民国")
                self.assertEqual(
                    exceptional["world.era"], f"exception:{exception_kind}")

    def test_exception_cannot_be_declared_as_wildcard(self):
        with self.assertRaises(RuleScopeError):
            resolve_rules(
                _context(),
                system_base=[Rule(
                    "world.era",
                    "anything",
                    applicability={"story_phase": "*"},
                    exception_kind="dream",
                )],
            )

    def test_exception_kind_must_match_its_applicable_phase(self):
        with self.assertRaises(RuleScopeError):
            resolve_rules(
                _context(),
                system_base=[Rule(
                    "world.era",
                    "wrong",
                    applicability={"story_phase": "戏中戏"},
                    exception_kind="dream",
                )],
            )


class FingerprintAndFacadeTest(unittest.TestCase):
    def test_fingerprint_is_stable_across_input_order(self):
        first = resolve_rules(
            _context(),
            system_base=[
                Rule("b.rule", {"x": 2}),
                Rule("a.rule", {"x": 1}),
            ],
        )
        second = resolve_rules(
            _context(),
            system_base=[
                Rule("a.rule", {"x": 1}),
                Rule("b.rule", {"x": 2}),
            ],
        )

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertRegex(first.fingerprint, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.as_dict()["fingerprint"], first.fingerprint)

    def test_fingerprint_changes_with_bound_context(self):
        rules = [Rule("frame.no_subtitles", True)]
        first = resolve_rules(_context(shot_no=7), system_base=rules)
        second = resolve_rules(_context(shot_no=8), system_base=rules)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_reusable_resolver_keeps_director_call_small(self):
        resolver = RuleResolver(
            technical_hard=[Rule("frame.no_subtitles", True)],
            system_base=[Rule("camera.motion", "static")],
        )
        result = resolver.resolve(
            _context(),
            current_shot=RuleBundle(
                [Rule("camera.motion", "push")],
                project_id=PROJECT,
                episode_id=EPISODE,
                source="shot-contract",
            ),
        )
        self.assertEqual(
            result.rules,
            {"camera.motion": "push", "frame.no_subtitles": True},
        )

    def test_storage_compatibility_facade_accepts_friendly_rule_packs(self):
        stack = resolve_rule_stack(
            context={
                "project_id": 101,
                "episode_id": 12,
                "stage": "storyboard",
                "modality": "image",
                "shot_no": 7,
                "scene_no": 2,
                "active_story_phase": "梦境",
                "active_realm_id": "dream-hotel",
                "event_id": "event-7",
                "era": "民国",
            },
            technical_rules=[
                {"key": "technical.no_subtitles", "value": True},
            ],
            base_rules={
                "scope": "system_base",
                "rules": [
                    {"key": "camera.motion", "text": "static"},
                    {"key": "creative.base_text", "text": "基础文本"},
                    {"key": "disabled.rule", "text": "不生效",
                     "enabled": False},
                ],
            },
            project_rules={
                "scope": "project_series",
                "version": 3,
                "rules": [{
                    "key": "camera.motion",
                    "text": "project push",
                }, {
                    "key": "world.phase_marker",
                    "text": "梦境酒店事件",
                    "applicability": {
                        "stages": ["storyboard"],
                        "modalities": ["image"],
                        "shot_nos": [7],
                        "scene_nos": [2],
                        "story_phases": ["梦境"],
                        "eras": ["民国"],
                        "active_realm_ids": ["dream-hotel"],
                        "event_ids": ["event-7"],
                    },
                }],
            },
            episode_rules={
                "scope": "episode_temporary",
                "rules": [{"key": "camera.motion", "value": "episode pan"}],
            },
            shot_rules={
                "scope": "current_shot",
                "rules": [
                    {"key": "camera.motion", "text": "shot closeup"},
                    {"key": "technical.no_subtitles", "value": False},
                ],
            },
        )

        self.assertEqual(stack["effective_rules"]["camera.motion"],
                         "shot closeup")
        self.assertEqual(stack["effective_rules"]["creative.base_text"],
                         "基础文本")
        self.assertEqual(stack["effective_rules"]["world.phase_marker"],
                         "梦境酒店事件")
        self.assertIs(stack["effective_rules"]["technical.no_subtitles"], True)
        self.assertNotIn("disabled.rule", stack["effective_rules"])
        self.assertEqual(stack["sources"]["world.phase_marker"]["source"],
                         "project_series:v3")
        self.assertEqual(stack["suppressed"], stack["overridden"])
        self.assertEqual(stack["effective_rules"], stack["final_rules"])
        self.assertRegex(stack["fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_storage_facade_rejects_explicit_foreign_binding(self):
        with self.assertRaises(ScopeBindingError):
            resolve_rule_stack(
                context={"project_id": PROJECT, "episode_id": EPISODE},
                project_rules={
                    "scope": "project_series",
                    "project_id": "foreign-project",
                    "rules": [{"key": "visual.palette", "text": "red"}],
                },
            )


if __name__ == "__main__":
    unittest.main()
