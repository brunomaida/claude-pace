"""Behaviour tests for hooks/pacing.py (weekly pacing cadencer; default OFF)."""
from __future__ import annotations
import datetime, json, os, subprocess, sys, tempfile, time, unittest
from unittest import mock
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path[:0] = [str(HOOKS)]
import pacing  # noqa: E402

# Reset Thu 2026-10-15 09:00Z; window = Thu 2026-10-08 09:00Z .. reset; "now" = Sat 2026-10-10 14:19Z.
RESET7 = 1792054800
NOW = 1791641943
EXEMPT = ['exempt-reviewer', 'exempt-worker']  # neutral names: no test depends on the owner's private default
CFG = dict(pacing.DEFAULTS, exempt_agents=EXEMPT)


def _sample(ts, u5, u7=71, r5=1791648600):
    return {'ts': ts, 'u5': u5, 'u7': u7, 'r5': r5, 'r7': RESET7}


class BizSeconds(unittest.TestCase):
    def test_fullWeekWindow_isFiveBusinessDays(self):
        total = pacing.biz_seconds(RESET7 - 7 * 86400, RESET7, 0, {0, 1, 2, 3, 4}, 0, 24)
        self.assertEqual(total, 5 * 86400)

    def test_weekendOnly_isZero(self):
        sat = 1791590400  # Sat 2026-10-10 00:00Z
        self.assertEqual(pacing.biz_seconds(sat, sat + 2 * 86400, 0, {0, 1, 2, 3, 4}, 0, 24), 0)

    def test_businessHoursWindow_countsOnlyThoseHours(self):
        total = pacing.biz_seconds(RESET7 - 7 * 86400, RESET7, 0, {0, 1, 2, 3, 4}, 9, 18)
        self.assertEqual(total, 5 * 9 * 3600)


class Decide(unittest.TestCase):
    def test_noSamples_isNa(self):
        self.assertEqual(pacing.decide(NOW, [], CFG)['state'], 'na')

    def test_missingRateLimits_isNa(self):
        self.assertEqual(pacing.decide(NOW, [{'ts': NOW, 'u5': None, 'u7': None, 'r5': None, 'r7': None}], CFG)['state'], 'na')

    def test_oldSample_isStale(self):
        self.assertEqual(pacing.decide(NOW, [_sample(NOW - 700, 37)], CFG)['state'], 'stale')

    def test_aheadOfBudget_leadMatchesFormula(self):
        dec = pacing.decide(NOW, [_sample(NOW, 37, 71)], CFG)
        elapsed = 39 * 3600  # Thu 09:00Z..Sat 14:19Z minus weekend
        budget = 95 * (15 * 3600 + 86400) / (5 * 86400)
        self.assertEqual(dec['state'], 'L1')
        self.assertAlmostEqual(dec['lead'], 71 - budget, places=3)
        self.assertGreater(dec['lead'], CFG['deadband_pct'])

    def test_behindBudget_isOkWithNoDelay(self):
        dec = pacing.decide(NOW, [_sample(NOW, 10, 20)], CFG)
        self.assertEqual((dec['state'], dec['delay_rec']), ('ok', 0))

    def test_idle_noDelayEvenWhenAhead(self):
        dec = pacing.decide(NOW, [_sample(NOW - 1500, 37), _sample(NOW, 37)], CFG)
        self.assertEqual(dec['delay_rec'], 0)

    def test_burst_producesDelay(self):
        pts = [_sample(NOW - 1500, 30), _sample(NOW - 600, 36), _sample(NOW, 40)]
        dec = pacing.decide(NOW, pts, CFG)
        self.assertGreater(dec['delay_rec'], 0)
        self.assertLessEqual(dec['delay_rec'], CFG['cap_s'])

    def test_currentRate_neededRate_andProposedReduction(self):
        pts = [_sample(NOW - 1500, 30), _sample(NOW - 600, 36), _sample(NOW, 40)]
        dec = pacing.decide(NOW, pts, CFG)
        self.assertAlmostEqual(dec['now_rate'], 2.4, places=2)  # 4% of 5h x r 0.1 over 10 min, in weekly %/h
        self.assertAlmostEqual(dec['need_rate'], dec['allow_day'] / 24, places=6)
        self.assertEqual(dec['reduce_pct'], round(100 * (1 - dec['need_rate'] / dec['now_rate'])))
        self.assertEqual(set(dec['rates']), {10, 15, 25, 30})

    def test_idleSamples_rateIsZeroAndNoReduction(self):
        dec = pacing.decide(NOW, [_sample(NOW - 1500, 37), _sample(NOW, 37)], CFG)
        self.assertEqual((dec['now_rate'], dec['reduce_pct']), (0.0, 0))

    def test_staleIdleSessionSample_doesNotInflateDelay(self):
        # an idle session still reports u5=40 (same r5) between samples of the active session at 50
        pts = [_sample(NOW - 1500, 50), _sample(NOW - 1200, 40), _sample(NOW - 600, 50), _sample(NOW, 50)]
        dec = pacing.decide(NOW, pts, CFG)
        self.assertEqual(set(dec['trend'].values()), {0})
        self.assertEqual((dec['delay_rec'], dec['deficit']), (0, 0))

    def test_staleIdleSessionSample_withFiveHourAxis_doesNotInflateDelay(self):
        pts = [_sample(NOW - 1500, 90), _sample(NOW - 1200, 80), _sample(NOW - 600, 90), _sample(NOW, 90)]
        dec = pacing.decide(NOW, pts, CFG, axes=('wk', 'h5'))
        self.assertEqual((dec['delay_5h'], dec['deficit5']), (0, 0))

    def test_staleSampleAfterGrowth_keepsRealGrowth(self):
        # real growth 30 -> 40 stays visible while a stale 30 shows up after it
        pts = [_sample(NOW - 1500, 30), _sample(NOW - 900, 40), _sample(NOW - 600, 30), _sample(NOW, 40)]
        dec = pacing.decide(NOW, pts, CFG)
        self.assertEqual(dec['trend'][25], 10)

    def test_singleSample_rateUnknown(self):
        dec = pacing.decide(NOW, [_sample(NOW, 37)], CFG)
        self.assertIsNone(dec['now_rate'])

    def test_deadClampKeys_areGone_andCapStillBinds(self):
        self.assertNotIn('deficit_max_pct', pacing.DEFAULTS)
        self.assertNotIn('deficit5_max_pct', pacing.DEFAULTS)
        cfg = dict(CFG, k_s_per_pct=1000, k5_s_per_pct=1000)
        pts = [_sample(NOW - 1500, 10), _sample(NOW, 60)]
        dec = pacing.decide(NOW, pts, cfg, axes=('wk', 'h5'))
        self.assertEqual((dec['delay_wk'], dec['delay_5h']), (cfg['cap_s'], cfg['cap_s']))

    def test_huge_burst_isClampedAtCap(self):
        cfg = dict(CFG, k_s_per_pct=1000)
        pts = [_sample(NOW - 1500, 10), _sample(NOW, 60)]
        self.assertEqual(pacing.decide(NOW, pts, cfg)['delay_rec'], cfg['cap_s'])

    def test_fiveHourFull_isLevel3AtCap(self):
        dec = pacing.decide(NOW, [_sample(NOW, 96)], CFG)
        self.assertEqual((dec['level'], dec['delay_rec']), (3, CFG['cap_s']))

    def test_sustainedCap_escalatesToLevel2(self):
        cfg = dict(CFG, k_s_per_pct=1000)
        pts = [_sample(NOW - 1500, 10), _sample(NOW, 60)]
        d1 = pacing.decide(NOW, pts, cfg)
        d2 = pacing.decide(NOW + 30, pts, cfg, d1)
        self.assertEqual((d1['level'], d2['level']), (1, 2))

    def test_level2IsStickyThenReleases(self):
        prev = {'level': 2, 'cap_ticks': 0, 'last_cap_ts': NOW - 60}
        sticky = pacing.decide(NOW, [_sample(NOW, 40)], CFG, prev)
        prev['last_cap_ts'] = NOW - 16 * 60
        released = pacing.decide(NOW, [_sample(NOW, 40)], CFG, prev)
        self.assertEqual((sticky['level'], released['level']), (2, 1))

    def _escalation(self, rec_target, mult):
        pts = [_sample(NOW - 1500, 10), _sample(NOW, 60)]
        deficit = pacing.decide(NOW, pts, dict(CFG, k_s_per_pct=1))['delay_wk']
        cfg = dict(CFG, k_s_per_pct=rec_target / deficit)
        d1 = pacing.decide(NOW, pts, cfg, mult=mult)
        d2 = pacing.decide(NOW + 30, pts, cfg, d1, mult=mult)
        return d1['delay_rec'], d2['level']

    def test_atCap_followsTheStepNotTheRecommendation(self):
        rec, level_half = self._escalation(CFG['cap_s'], 0.5)  # recommends cap, sleeps half of it
        self.assertAlmostEqual(rec, CFG['cap_s'])
        self.assertEqual(level_half, 1)
        _, level_triple = self._escalation(20, 3.0)  # recommends 20 s, sleeps the cap
        self.assertEqual(level_triple, 2)

    def test_level2_holdsWhenPressureClears_thenReleasesToOk(self):
        prev = {'level': 2, 'cap_ticks': 0, 'last_cap_ts': NOW - 60}
        under = [_sample(NOW, 10, 20)]  # far under budget: no lead, no deficit
        held = pacing.decide(NOW, under, CFG, prev)
        prev['last_cap_ts'] = NOW - 16 * 60
        released = pacing.decide(NOW, under, CFG, prev)
        self.assertEqual((held['level'], released['level']), (2, 0))

    def test_fiveHourAxis_deficitInsideDeadband_staysOkWithNoDelay(self):
        pts = [_sample(NOW - 120, 50, r5=int(NOW + 7200)), _sample(NOW, 51, r5=int(NOW + 7200))]
        dec = pacing.decide(NOW, pts, CFG, axes=('h5',))
        self.assertTrue(0 < dec['deficit5'] <= CFG['deadband5_pct'])
        self.assertEqual((dec['level'], dec['delay_5h'], dec['delay_rec']), (0, 0, 0))

    def test_fiveHourAxis_insideDeadband_showsNoBehindPaceForm(self):
        pts = [_sample(NOW - 120, 50, r5=int(NOW + 7200)), _sample(NOW, 51, r5=int(NOW + 7200))]
        dec = pacing.decide(NOW, pts, CFG, axes=('h5',))
        ctl = {'h5': {'on': True, 'measure': False}}
        self.assertNotIn('>', pacing.render_segment(dec, ctl, CFG, NOW))
        self.assertNotIn('>', pacing.cadence_fields(dec, ctl, CFG, NOW)['h5'][0])

    def test_fiveHourAxis_staysInLevel1UntilDeficitIsZero(self):
        pts = [_sample(NOW - 120, 50, r5=int(NOW + 7200)), _sample(NOW, 51, r5=int(NOW + 7200))]
        held = pacing.decide(NOW, pts, CFG, {'level': 1, 'axes': ['h5']}, axes=('h5',))
        fresh = pacing.decide(NOW, pts, CFG, {'level': 0, 'axes': ['h5']}, axes=('h5',))
        self.assertEqual((held['level'], fresh['level']), (1, 0))
        self.assertGreater(held['delay_5h'], 0)

    def test_fiveHourAxis_deficitAboveDeadband_entersLevel1(self):
        pts = [_sample(NOW - 120, 50, r5=int(NOW + 7200)), _sample(NOW, 53, r5=int(NOW + 7200))]
        dec = pacing.decide(NOW, pts, CFG, axes=('h5',))
        self.assertGreater(dec['deficit5'], CFG['deadband5_pct'])
        self.assertEqual(dec['level'], 1)
        self.assertGreater(dec['delay_5h'], 0)

    def test_fiveHourAxis_needRateIsCeilingLeftOverHoursLeft(self):
        dec = pacing.decide(NOW, [_sample(NOW, 40)], CFG)
        hours = (1791648600 - NOW) / 3600
        self.assertAlmostEqual(dec['need_rate5'], (CFG['five_hour_block_pct'] - 40) / hours, places=6)

    def test_fiveHourAxis_burstInsideWindow_producesDelay5(self):
        r5 = int(NOW + 3600)  # 85% used, 1 h left: need 10 %/h, burning 24 %/h
        pts = [_sample(NOW - 600, 81, r5=r5), _sample(NOW, 85, r5=r5)]
        dec = pacing.decide(NOW, pts, CFG, axes=('h5',))
        self.assertGreater(dec['delay_5h'], 0)
        self.assertLessEqual(dec['delay_5h'], CFG['cap_s'])
        self.assertEqual((dec['level'], dec['delay_rec']), (1, dec['delay_5h']))
        self.assertAlmostEqual(dec['now_rate5'], 24.0, places=1)
        self.assertEqual(dec['reduce5_pct'], round(100 * (1 - dec['need_rate5'] / dec['now_rate5'])))

    def test_fiveHourAxis_slowUse_noDelay(self):
        r5 = int(NOW + 3 * 3600)
        pts = [_sample(NOW - 600, 10, r5=r5), _sample(NOW, 11, r5=r5)]
        dec = pacing.decide(NOW, pts, CFG, axes=('h5',))
        self.assertEqual((dec['delay_5h'], dec['level']), (0, 0))

    def test_engagedAxes_pickMaxAndInactiveAxisContributesNothing(self):
        r5 = int(NOW + 3600)
        pts = [_sample(NOW - 600, 81, r5=r5), _sample(NOW, 85, r5=r5)]
        both = pacing.decide(NOW, pts, CFG, axes=('wk', 'h5'))
        self.assertEqual(both['delay_rec'], max(both['delay_wk'], both['delay_5h']))
        wk_only = pacing.decide(NOW, pts, CFG, axes=('wk',))
        self.assertEqual(wk_only['delay_rec'], wk_only['delay_wk'])
        self.assertEqual(wk_only['delay_5h'], both['delay_5h'])

    def test_fiveHourFull_forcesBothAxesToCap(self):
        dec = pacing.decide(NOW, [_sample(NOW, 96)], CFG)
        self.assertEqual((dec['delay_wk'], dec['delay_5h']), (CFG['cap_s'], CFG['cap_s']))

    def test_driveAxes_actingWinsOverMeasuring(self):
        self.assertEqual(pacing.drive_axes({'wk': {'on': True}, 'h5': {'measure': True}}), ('wk',))
        self.assertEqual(pacing.drive_axes({'h5': {'measure': True}}), ('h5',))
        self.assertEqual(pacing.drive_axes({}), ('wk',))

    def test_measuringAxis_neverEscalatesActingAxisToLevel2(self):
        r5 = int(NOW + 3600)
        pts = [_sample(NOW - 600, 70, u7=10, r5=r5), _sample(NOW, 85, u7=10, r5=r5)]
        cfg = dict(CFG, k5_s_per_pct=1000)
        axes = pacing.drive_axes({'wk': {'on': True}, 'h5': {'measure': True}})
        d1 = pacing.decide(NOW, pts, cfg, axes=axes)
        d2 = pacing.decide(NOW + 30, pts, cfg, d1, axes=axes)
        self.assertEqual((d1['level'], d2['level']), (0, 0))

    def test_axisChange_dropsStickyEscalation(self):
        r5 = int(NOW + 3600)
        pts = [_sample(NOW - 600, 70, r5=r5), _sample(NOW, 85, r5=r5)]
        cfg = dict(CFG, k5_s_per_pct=1000)
        d1 = pacing.decide(NOW, pts, cfg, axes=('h5',))
        d2 = pacing.decide(NOW + 30, pts, cfg, d1, axes=('h5',))
        d3 = pacing.decide(NOW + 60, pts, cfg, d2, axes=('wk',))
        self.assertEqual((d2['level'], d3['level']), (2, 1))

    def test_fiveHourAxis_missingOrExpiredWindow_isSafe(self):
        no_r5 = dict(_sample(NOW, 40), r5=None)
        self.assertIsNone(pacing.decide(NOW, [no_r5], CFG, axes=('h5',))['need_rate5'])
        past = [_sample(NOW - 600, 80, r5=int(NOW - 5)), _sample(NOW, 84, r5=int(NOW - 5))]
        dec = pacing.decide(NOW, past, CFG, axes=('h5',))
        self.assertIsNone(dec['need_rate5'])  # the window reset: nothing to pace against until a new reading
        self.assertEqual(dec['delay_5h'], 0)


class Control(unittest.TestCase):
    def test_stepClampsAtLadderEnds(self):
        ctl = {'on': True, 'step': pacing.default_step(CFG)}
        for _ in range(10): ctl = pacing.apply_ctl('up', ctl, CFG)
        self.assertEqual(ctl['step'], len(CFG['steps']) - 1)
        for _ in range(10): ctl = pacing.apply_ctl('down', ctl, CFG)
        self.assertEqual(ctl['step'], 0)

    def test_autoReturnsToSuggestedStepAndKeepsAxes(self):
        ctl = {'wk': {'on': True}, 'h5': {'on': False, 'measure': True}, 'step': 5, 'template': 'L2'}
        out = pacing.apply_ctl('auto', ctl, CFG)
        self.assertEqual(out['step'], pacing.default_step(CFG))
        self.assertEqual((out['wk']['on'], out['h5']['on'], out['h5']['measure'], out['template']),
                         (True, False, True, 'L2'))

    def test_isManual_onlyWhenStepLeavesTheSuggestion(self):
        base = pacing.default_step(CFG)
        self.assertEqual([pacing.is_manual({}, CFG), pacing.is_manual({'step': base}, CFG),
                          pacing.is_manual({'step': base + 1}, CFG), pacing.is_manual({'step': base - 1}, CFG)],
                         [False, False, True, True])

    def test_upThenAuto_leavesManualAndBack(self):
        ctl = pacing.apply_ctl('up', {'on': True}, CFG)
        self.assertTrue(pacing.is_manual(ctl, CFG))
        self.assertFalse(pacing.is_manual(pacing.apply_ctl('auto', ctl, CFG), CFG))

    def test_parseAuto(self):
        self.assertEqual([pacing.parse_pace_prompt('/pace auto'), pacing.parse_pace_prompt('pace AUTO')],
                         ['auto', 'auto'])
        self.assertIsNone(pacing.parse_pace_prompt('pace 5h auto'))  # step is one global ladder

    def test_resetTurnsOnAtRecommended(self):
        ctl = pacing.apply_ctl('reset', {'on': False, 'step': 0}, CFG)
        self.assertEqual((ctl['wk']['on'], ctl['h5']['on'], CFG['steps'][ctl['step']]), (True, True, 1.0))

    def test_effectiveDelay_offIsZero_multiplierAndCap(self):
        dec = {'delay_rec': 20}
        base = pacing.default_step(CFG)
        self.assertEqual(pacing.effective_delay(dec, {'on': False, 'step': base}, CFG), 0)
        self.assertEqual(pacing.effective_delay(dec, {'on': True, 'step': base}, CFG), 20)
        self.assertEqual(pacing.effective_delay(dec, {'on': True, 'step': base + 1}, CFG), 30)  # x1.5
        self.assertEqual(pacing.effective_delay(dec, {'on': True, 'step': base + 3}, CFG), CFG['cap_s'])  # x3 capped

    def test_effectiveDelay_takesMaxOfActingAxesOnly(self):
        dec = {'delay_wk': 10, 'delay_5h': 30, 'delay_rec': 30}
        base = pacing.default_step(CFG)
        wk, h5 = {'wk': {'on': True}, 'step': base}, {'h5': {'on': True}, 'step': base}
        both = {'wk': {'on': True}, 'h5': {'on': True}, 'step': base}
        self.assertEqual([pacing.effective_delay(dec, c, CFG) for c in (wk, h5, both)], [10, 30, 30])
        self.assertEqual(pacing.effective_delay(dec, {'wk': {'measure': True}, 'step': base}, CFG), 0)

    def test_promptParser(self):
        p = pacing.parse_pace_prompt
        self.assertEqual(p('/pace up'), 'up')
        self.assertEqual(p('  PACE   off '), 'off')
        self.assertEqual(p('pace'), 'status')
        self.assertIsNone(p('pace yourself please'))
        self.assertIsNone(p('implement pace limiter'))

    def test_promptParser_measure(self):
        p = pacing.parse_pace_prompt
        self.assertEqual(p('pace measure'), 'measure-on')
        self.assertEqual(p('/pace measure on'), 'measure-on')
        self.assertEqual(p('pace  measure   off'), 'measure-off')

    def test_measureIsIndependentOfReduction(self):
        ctl = pacing.apply_ctl('measure-on', {}, CFG)
        self.assertEqual((ctl['wk']['measure'], ctl['wk']['on']), (True, False))
        ctl = pacing.apply_ctl('on', ctl, CFG)
        ctl = pacing.apply_ctl('off', ctl, CFG)
        self.assertEqual((ctl['wk']['measure'], ctl['wk']['on']), (True, False))
        self.assertFalse(pacing.apply_ctl('measure-off', ctl, CFG)['wk']['measure'])

    def test_legacyCtl_readsAsWeekly(self):
        c = pacing.norm_ctl({'on': True, 'measure': True, 'step': 2})
        self.assertEqual((c['wk'], c['h5'], c['step']),
                         ({'on': True, 'measure': True}, {'on': False, 'measure': False}, 2))
        self.assertNotIn('on', c)

    def test_axisCommands_touchOnlyThatAxis(self):
        ctl = pacing.apply_ctl('h5:on', {}, CFG)
        self.assertEqual((ctl['h5']['on'], ctl['wk']['on']), (True, False))
        ctl = pacing.apply_ctl('wk:measure-on', ctl, CFG)
        self.assertEqual((ctl['wk']['measure'], ctl['h5']['measure']), (True, False))

    def test_bareOnOffAndMeasure_touchBothAxes(self):
        ctl = pacing.apply_ctl('on', {}, CFG)
        self.assertEqual((ctl['wk']['on'], ctl['h5']['on']), (True, True))
        ctl = pacing.apply_ctl('measure-on', pacing.apply_ctl('off', ctl, CFG), CFG)
        self.assertEqual([ctl['wk']['on'], ctl['h5']['on'], ctl['wk']['measure'], ctl['h5']['measure']],
                         [False, False, True, True])

    def test_axisSets(self):
        ctl = {'wk': {'measure': True}, 'h5': {'on': True}}
        self.assertEqual(pacing.acting_axes(ctl), ['h5'])
        self.assertEqual(pacing.engaged_axes(ctl), ['wk', 'h5'])
        self.assertEqual(pacing.engaged_axes({}), [])

    def test_promptParser_axis(self):
        p = pacing.parse_pace_prompt
        self.assertEqual(p('/pace wk on'), 'wk:on')
        self.assertEqual(p('pace weekly off'), 'wk:off')
        self.assertEqual(p('pace 5h measure'), 'h5:measure-on')
        self.assertEqual(p('pace  5H  measure   off'), 'h5:measure-off')
        self.assertIsNone(p('pace 5h up'))
        self.assertIsNone(p('pace wk'))


class RateFormat(unittest.TestCase):
    def test_fromOneUp_isInteger(self):
        f = pacing.fmt_rate
        self.assertEqual([f(12.0), f(1.4), f(1.0), f(0.96), f(2.5)], ['12', '1', '1', '1', '3'])

    def test_belowOne_isOneDecimal(self):
        f = pacing.fmt_rate
        self.assertEqual([f(0.8), f(0.04), f(0.0)], ['0.8', '0.0', '0.0'])


class Render(unittest.TestCase):
    def _dec(self, **kw):
        base = {'state': 'L1', 'level': 1, 'lead': 33.4, 'delay_rec': 25, 'allow_day': 8.04, 'r5': NOW + 2280}
        base.update(kw)
        return base

    def _rates(self, **kw):
        return self._dec(now_rate=1.8, need_rate=0.3, reduce_pct=83, **kw)

    def _on(self, extra=0):
        return {'on': True, 'step': pacing.default_step(CFG) + extra}

    def test_defaultOff_showsLeadOnly(self):
        self.assertEqual(pacing.render_segment(self._rates(), {}, CFG, NOW), 'cadence: [off] +33%')

    def test_okWithinDeadband(self):
        dec = self._dec(state='ok', level=0, lead=1.0, delay_rec=0)
        self.assertEqual(pacing.render_segment(dec, self._on(), CFG, NOW), 'cadence: [ok] r:wk')

    def test_okWithSlack_showsNegativeLead(self):
        dec = self._dec(state='ok', level=0, lead=-6.2, delay_rec=0)
        self.assertEqual(pacing.render_segment(dec, self._on(), CFG, NOW), 'cadence: [ok] week:-6% r:wk')

    def test_level1_withDelay(self):
        self.assertEqual(pacing.render_segment(self._dec(), self._on(), CFG, NOW), 'cadence: [L1] week:+33% 25s r:wk')

    def test_level1_withStepMultiplier(self):
        self.assertEqual(pacing.render_segment(self._dec(), self._on(1), CFG, NOW), 'cadence: [L1] week:+33% 38s manual x1.5 r:wk')

    def test_level2_atCap_showsConcurrencyAndAllow(self):
        out = pacing.render_segment(self._dec(state='L2', level=2, delay_rec=45), self._on(), CFG, NOW, active=2)
        self.assertEqual(out, 'cadence: [L2] week:+33% 45s! 2/2 8%/d r:wk')

    def test_level3_showsReset(self):
        out = pacing.render_segment(self._dec(state='L3', level=3, delay_rec=45), self._on(), CFG, NOW)
        self.assertEqual(out, 'cadence: [L3] 45s! 5h:38m r:wk')

    def test_staleAndNa(self):
        self.assertEqual(pacing.render_segment({'state': 'stale'}, {'on': True}, CFG, NOW), 'cadence: [?] r:wk')
        self.assertEqual(pacing.render_segment({'state': 'na'}, {'on': True}, CFG, NOW), 'cadence: [n/a] r:wk')
        self.assertEqual(pacing.render_segment({'state': 'stale'}, {}, CFG, NOW), 'cadence: [off]')
        self.assertEqual(pacing.render_segment({'state': 'stale'}, {'measure': True}, CFG, NOW), 'cadence: [?] r:off')

    def test_measureOnly_showsRatesProposalAndRedOff(self):
        out = pacing.render_segment(self._rates(), {'measure': True}, CFG, NOW)
        self.assertEqual(out, 'cadence: [L1] week:+33% 2>0.3%/h -83% ~25s r:off')

    def test_acting_showsRatesAppliedDelayAndRedOn(self):
        out = pacing.render_segment(self._rates(), self._on(), CFG, NOW)
        self.assertEqual(out, 'cadence: [L1] week:+33% 2>0.3%/h -83% 25s r:wk')

    def test_measureOk_showsCurrentRateOnly(self):
        dec = self._dec(state='ok', level=0, lead=-6.2, delay_rec=0, now_rate=0.2, need_rate=0.3, reduce_pct=0)
        self.assertEqual(pacing.render_segment(dec, {'measure': True}, CFG, NOW), 'cadence: [ok] week:-6% 0.2%/h r:off')

    def test_measureOnly_level3_marksProposedDelay(self):
        out = pacing.render_segment(self._dec(state='L3', level=3, delay_rec=45), {'measure': True}, CFG, NOW)
        self.assertEqual(out, 'cadence: [L3] ~45s! 5h:38m r:off')

    def test_measureOnly_ignoresStepMultiplier(self):
        ctl = {'measure': True, 'step': pacing.default_step(CFG) + 1}
        self.assertEqual(pacing.render_segment(self._dec(), ctl, CFG, NOW), 'cadence: [L1] week:+33% ~25s r:off')

    def test_rateFormat_integerFromOneUp_oneDecimalBelow(self):
        dec = self._dec(state='ok', level=0, lead=-6.2, delay_rec=0, now_rate=0.8, need_rate=2.0, reduce_pct=0)
        self.assertEqual(pacing.render_segment(dec, {'wk': {'measure': True}}, CFG, NOW), 'cadence: [ok] week:-6% 0.8%/h r:off')

    def test_fiveHourAxis_onShowsRateReductionAndAxisInR(self):
        dec = self._dec(delay_rec=20, delay_5h=20, deficit5=2.3, now_rate5=14.2, need_rate5=6.0, reduce5_pct=58)
        out = pacing.render_segment(dec, {'h5': {'on': True}}, CFG, NOW)
        self.assertEqual(out, 'cadence: [L1] 5h:14>6%/h -58% 20s r:5h')

    def test_bothAxes_separatedByBarAndRListsBoth(self):
        dec = self._dec(delay_rec=30, delay_wk=12, delay_5h=30, now_rate=12.0, need_rate=8.0, reduce_pct=33,
                        deficit5=1.0, now_rate5=14.0, need_rate5=0.6, reduce5_pct=96)
        ctl = {'wk': {'on': True}, 'h5': {'on': True}}
        self.assertEqual(pacing.render_segment(dec, ctl, CFG, NOW),
                         'cadence: [L1] week:+33% 12>8%/h -33% . 5h:14>0.6%/h -96% 30s r:wk+5h')

    def test_fiveHourCausesL1_weeklyUnderBudget_showsNoWeeklyReduction(self):
        dec = self._dec(lead=-36.0, delay_rec=20, delay_wk=0, delay_5h=20, now_rate=9.0, need_rate=1.0,
                        reduce_pct=89, deficit5=2.0, now_rate5=14.0, need_rate5=6.0, reduce5_pct=57)
        ctl = {'wk': {'on': True}, 'h5': {'on': True}}
        self.assertEqual(pacing.render_segment(dec, ctl, CFG, NOW),
                         'cadence: [L1] week:-36% 9%/h . 5h:14>6%/h -57% 20s r:wk+5h')

    def test_fiveHourAxis_noDeficit_showsCurrentRateOnly(self):
        dec = self._dec(state='ok', level=0, delay_rec=0, deficit5=0.0, now_rate5=3.0, need_rate5=19.0)
        self.assertEqual(pacing.render_segment(dec, {'h5': {'measure': True}}, CFG, NOW), 'cadence: [ok] 5h:3%/h r:off')


class Templates(unittest.TestCase):
    """Status line layouts L1, L2, C1, C2 (golden strings come from the S1.4 request)."""
    GIT = 'claude-config/develop (a1b2c3d)'
    VIEW = {'model': 'Sonnet 5.5 (High)', 'ctx': 42, 'git': GIT, 'five': (63, 7800), 'week': (58, 273600)}
    DEC = {'state': 'L1', 'level': 1, 'lead': 8.0, 'delay_rec': 12, 'delay_wk': 12, 'delay_5h': 12,
           'now_rate': 3.0, 'need_rate': 2.0, 'reduce_pct': 33,
           'deficit5': 1.0, 'now_rate5': 4.0, 'need_rate5': 3.0, 'reduce5_pct': 25}
    BOTH = {'wk': {'on': True}, 'h5': {'on': True}}

    def _r(self, name, dec=None, ctl=None, view=None):
        return pacing.render_template(name, view or self.VIEW, dec or self.DEC, self.BOTH if ctl is None else ctl,
                                      CFG, NOW)

    def test_L1_goldenSample(self):
        self.assertEqual(self._r('L1'), '\n'.join([
            'Sonnet 5.5 (High) |  context 42%  |  claude-config/develop (a1b2c3d)',
            '5h ▓▓▓▓▓▓░░░░ 63% (2h 10m)  |  week ▓▓▓▓▓▓░░░░ 58% (3d 4h)  |  [L1] +8% over  delay 12s']))

    def test_L2_goldenSample_columnsAligned(self):
        self.assertEqual(self._r('L2'), '\n'.join([
            'Sonnet 5.5 (High) | context 42%  | cadence [L1] r:wk+5h | claude-config/develop (a1b2c3d)',
            '5h    ▓▓▓▓▓▓░░░░ 63% (2h 10m left)   pace 4>3 %/h  -25%   delay 12s',
            'week  ▓▓▓▓▓▓░░░░ 58% (3d 4h left)    pace 3>2 %/h  -33%   +8% over budget']))

    def test_C1_goldenSample(self):
        self.assertEqual(self._r('C1'), '\n'.join([
            'Sonnet 5.5 (High) | ctx 42% | claude-config/develop (a1b2c3d)',
            '5h 63% 2h10m . wk 58% 3d4h > [L1] +8% 12s r:wk+5h']))

    def test_C2_goldenSample(self):
        self.assertEqual(self._r('C2'), '\n'.join([
            'Sonnet 5.5 (High) | claude-config/develop (a1b2c3d)',
            '> ctx 42% | 5h 63% (2h10m) | week 58% (3d4h)',
            '> [L1] 5h 4>3%/h -25% 12s | wk +8% 3>2%/h -33%']))

    def test_L2_unevenWidths_stillAlignEveryColumn(self):
        dec = dict(self.DEC, now_rate=12.0, need_rate=8.0, reduce_pct=33, now_rate5=14.0, need_rate5=6.0, reduce5_pct=57)
        view = dict(self.VIEW, five=(100, 3540), week=(7, 30))
        rows = self._r('L2', dec=dec, view=view).split('\n')[1:]
        for token in ('pace', '-'):
            self.assertEqual(len({r.index(token, 7) for r in rows}), 1, rows)
        self.assertTrue(all(r == r.rstrip() for r in rows))

    def test_pacingOff_showsOffTagAndLeadOnly(self):
        out = self._r('C2', ctl={}).split('\n')
        self.assertEqual(out[2], '> [off] wk +8%')
        self.assertEqual(self._r('C1', ctl={}).split('\n')[1], '5h 63% 2h10m . wk 58% 3d4h > [off] +8%')
        self.assertEqual(self._r('L1', ctl={}).split('\n')[1].split('  |  ')[-1], '[off] +8% over')
        self.assertEqual(self._r('L2', ctl={}).split('\n')[0],
                         'Sonnet 5.5 (High) | context 42%  | cadence [off] | claude-config/develop (a1b2c3d)')

    def test_missingUsage_dropsTheSegmentsNotTheLine(self):
        view = {'model': 'Sonnet 5.5 (High)', 'ctx': None, 'git': '', 'five': None, 'week': None}
        self.assertEqual(self._r('C2', view=view), 'Sonnet 5.5 (High)\n> [L1] 5h 4>3%/h -25% 12s | wk +8% 3>2%/h -33%')
        self.assertEqual(self._r('C1', view=view), 'Sonnet 5.5 (High)\n[L1] +8% 12s r:wk+5h')

    def test_unappliedDelayIsMarkedAndMultiplierShown(self):
        ctl = {'wk': {'measure': True}, 'step': pacing.default_step(CFG) + 1}
        self.assertIn('delay ~12s', self._r('L1', ctl=ctl))
        out = self._r('C1', ctl=dict(self.BOTH, step=pacing.default_step(CFG) + 1))
        self.assertTrue(out.endswith('[L1] +8% 18s manual x1.5 r:wk+5h'), out)

    def test_barRoundsToNearestTenth(self):
        self.assertEqual([pacing.bar(p) for p in (0, 4, 5, 63, 58, 100, 140)],
                         ['░' * 10, '░' * 10, '▓' + '░' * 9, '▓' * 6 + '░' * 4, '▓' * 6 + '░' * 4, '▓' * 10, '▓' * 10])

    def test_remainingFormat(self):
        f = pacing.fmt_left
        self.assertEqual([f(7800), f(273600), f(2700), f(30), f(7800, True), f(273600, True)],
                         ['2h 10m', '3d 4h', '45m', '30s', '2h10m', '3d4h'])

    def test_templateOf_noneByDefaultAndForUnknownOrOff(self):
        self.assertEqual([pacing.template_of({}), pacing.template_of({'template': 'l2'}),
                          pacing.template_of({'template': 'zz'}), pacing.template_of({'template': 'OFF'})],
                         ['', 'L2', '', ''])

    def test_promptParse_template(self):
        p = pacing.parse_pace_prompt
        self.assertEqual([p('/pace template l2'), p('pace template C1'), p('pace template'), p('pace template off')],
                         ['tpl-l2', 'tpl-c1', 'status', 'tpl-off'])
        self.assertIsNone(p('pace template X9'))
        self.assertEqual(pacing.apply_ctl('tpl-l2', {}, CFG)['template'], 'L2')
        self.assertEqual(pacing.template_of(pacing.apply_ctl('tpl-off', {'template': 'L2'}, CFG)), '')

    def test_promptParse_templateAfter(self):
        p = pacing.parse_pace_prompt
        self.assertEqual([p('/pace template after'), p('pace TEMPLATE After')], ['tpl-after', 'tpl-after'])
        self.assertEqual(pacing.apply_ctl('tpl-after', {}, CFG)['template'], 'AFTER')

    def test_afterIsNotAFullLayout(self):
        self.assertEqual(pacing.template_of({'template': 'AFTER'}), '')
        self.assertTrue(pacing.after_of({'template': 'AFTER'}))
        self.assertEqual([pacing.after_of({}), pacing.after_of({'template': 'L2'}), pacing.after_of({'template': 'off'})],
                         [False, False, False])

    def test_statusNamesTheAfterTemplate(self):
        out = pacing.format_status({'state': 'na'}, {'template': 'AFTER'}, CFG, NOW)
        self.assertIn('| template after |', out)

    def test_weeklyOnly_delayBelongsToTheWeekNotTheFiveHourGroup(self):
        dec = dict(self.DEC, delay_5h=0)
        ctl = {'wk': {'on': True}}
        self.assertEqual(self._r('C2', dec=dec, ctl=ctl).split('\n')[2], '> [L1] wk +8% 3>2%/h -33% 12s')
        self.assertEqual(self._r('L2', dec=dec, ctl=ctl).split('\n')[1:], [
            '5h    ▓▓▓▓▓▓░░░░ 63% (2h 10m left)',
            'week  ▓▓▓▓▓▓░░░░ 58% (3d 4h left)    pace 3>2 %/h  -33%   +8% over budget delay 12s'])

    def test_bothAxesActing_delayFollowsTheDominantAxis(self):
        dec = dict(self.DEC, state='L2', level=2, delay_rec=30, delay_wk=30, delay_5h=0, deficit5=0.0, now_rate5=1.0)
        rows = lambda name, d=None: self._r(name, dec=d).splitlines()
        self.assertEqual(rows('C2', dec)[2], '> [L2] 5h 1%/h | wk +8% 3>2%/h -33% 30s 0/2')
        self.assertIn('+8% over budget delay 30s 0/2', rows('L2', dec)[2])
        self.assertIn('delay 12s', rows('L2')[1])  # equal delays stay on the 5h row (golden)

    def test_bothAxesActing_fiveHourDominant_andMeasureOnlyBoth(self):
        dec = dict(self.DEC, delay_rec=20, delay_wk=5, delay_5h=20)
        self.assertEqual(self._r('C2', dec=dec).splitlines()[2], '> [L1] 5h 4>3%/h -25% 20s | wk +8% 3>2%/h -33%')
        both = {'wk': {'measure': True}, 'h5': {'measure': True}}
        dec = dict(self.DEC, delay_rec=30, delay_wk=30, delay_5h=10)
        self.assertEqual(self._r('C2', dec=dec, ctl=both).splitlines()[2],
                         '> [L1] 5h 4>3%/h -25% | wk +8% 3>2%/h -33% ~30s')

    def test_formatStatus_showsTemplateOffByDefaultAndWhenOff(self):
        for ctl in ({}, {'template': 'OFF'}):
            self.assertIn('| template off |', pacing.format_status({'state': 'na'}, ctl, CFG, NOW))

    def test_fiveHourOnly_delayStaysOnTheFiveHourGroup(self):
        ctl = {'h5': {'on': True}}
        self.assertEqual(self._r('C2', ctl=ctl).split('\n')[2], '> [L1] 5h 4>3%/h -25% 12s | wk +8%')

    def test_L2_missingFiveHourWindow_keepsCadenceColumns(self):
        view = dict(self.VIEW, five=None)
        rows = self._r('L2', view=view).split('\n')[1:]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith('5h') and 'pace 4>3 %/h' in rows[0] and rows[0].endswith('delay 12s'), rows)

    def test_noDelay_showsRTagSoMeasureAndOnDiffer(self):
        dec = dict(self.DEC, state='ok', level=0, lead=1.0, delay_rec=0, delay_wk=0, delay_5h=0)
        self.assertTrue(self._r('C2', dec=dec).endswith(' r:wk+5h'))
        self.assertTrue(self._r('L1', dec=dec).endswith(' r:wk+5h'))

    def test_extrasShowWithoutDelay_andAllowGoesToTheWeek(self):
        dec = dict(self.DEC, state='L2', level=2, allow_day=8.04)
        out = self._r('L1', dec=dec, ctl=dict(self.BOTH, step=pacing.default_step(CFG) + 1))
        self.assertIn('delay 18s manual x1.5 0/2 8%/d', out)
        self.assertTrue(self._r('C2', dec=dec).split('\n')[2].endswith('| wk +8% 3>2%/h -33% 8%/d'))

    def test_contextCellKeepsTwoSpacesBeforeBarAtAnyWidth(self):
        for pct, cell in ((5, 'context 5%  | cadence'), (100, 'context 100%  | cadence')):
            head = self._r('L2', view=dict(self.VIEW, ctx=pct)).split('\n')[0]
            self.assertIn(cell, head)

    def test_multipleRepos_joinedWithEachTemplatesSeparator(self):
        view = dict(self.VIEW, git='a/b (1)\nc/d (2)')
        self.assertTrue(self._r('L1', view=view).split('\n')[0].endswith('context 42%  |  a/b (1)  |  c/d (2)'))
        self.assertTrue(self._r('C1', view=view).split('\n')[0].endswith('ctx 42% | a/b (1) | c/d (2)'))


class Slots(unittest.TestCase):
    """Subagent slots: atomic claim, FIFO confirm/release, exempt types never counted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = self.tmp.name
        self.cfg = dict(CFG, n_max=2)

    def tearDown(self): self.tmp.cleanup()

    def test_capBlocksOnlyWhenCapRequested(self):
        for _ in range(3): self.assertTrue(pacing.claim_agent(self.sd, NOW, self.cfg, cap=False))
        self.assertFalse(pacing.claim_agent(self.sd, time.time(), self.cfg, cap=True))

    def test_pendingClaimExpires_soDeniedSpawnDoesNotLeak(self):
        pacing.claim_agent(self.sd, time.time(), self.cfg, cap=False)
        later = time.time() + self.cfg['pending_ttl_s'] + 5
        self.assertEqual(pacing.count_agents(self.sd, later, self.cfg), 0)

    def test_startConfirmsAndStopReleasesOnlyConfirmed(self):
        pacing.claim_agent(self.sd, time.time(), self.cfg, cap=False)
        pacing.claim_agent(self.sd, time.time(), self.cfg, cap=False)
        pacing.agent_stopped(self.sd, self.cfg, 'general-purpose')  # nothing confirmed yet: no-op
        self.assertEqual(pacing.count_agents(self.sd, time.time(), self.cfg), 2)
        pacing.agent_started(self.sd, self.cfg, 'general-purpose')
        pacing.agent_stopped(self.sd, self.cfg, 'general-purpose')
        self.assertEqual(pacing.count_agents(self.sd, time.time(), self.cfg), 1)

    def test_exemptTypesNeverConfirmOrRelease(self):
        pacing.claim_agent(self.sd, time.time(), self.cfg, cap=False)
        pacing.agent_started(self.sd, self.cfg, EXEMPT[0])
        self.assertEqual(len(list(Path(self.sd, 'agents').glob('*.a'))), 0)
        pacing.agent_started(self.sd, self.cfg, 'general-purpose')
        pacing.agent_stopped(self.sd, self.cfg, EXEMPT[1])
        self.assertEqual(len(list(Path(self.sd, 'agents').glob('*.a'))), 1)


class FreshestSample(unittest.TestCase):
    def test_idleSessionWithOldValuesAndNewTs_doesNotWin(self):
        idle = dict(_sample(NOW, 30, 60), r5=1791640000)  # stale window, newest ts
        active = _sample(NOW - 10, 40, 71)
        dec = pacing.decide(NOW, sorted([active, idle], key=lambda s: s['ts']), CFG)
        self.assertEqual((dec['u7'], dec['u5']), (71, 40))


class Setup(unittest.TestCase):
    """`/pace setup`: write the status line a plugin cannot ship, without touching anything the user owns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self.tmp.name)
        self.settings = self.cfg_dir / 'settings.json'

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        return pacing.cmd_setup(str(self.cfg_dir), pacing.__file__)

    def test_parse_acceptsSetup(self):
        self.assertEqual(pacing.parse_pace_prompt('/pace setup'), 'setup')
        self.assertEqual(pacing.parse_pace_prompt('pace SETUP'), 'setup')
        self.assertEqual(pacing.parse_pace_prompt('pace setup silent'), 'setup-silent')

    def test_silentSetup_writesARecordOnlyCommandThatShowsNothing(self):
        pacing.cmd_setup(str(self.cfg_dir), pacing.__file__, silent=True)
        cmd = json.loads(self.settings.read_text(encoding='utf-8'))['statusLine']['command']
        self.assertIn('statusline --record-only', cmd)
        self.assertNotIn('--full', cmd)

    def test_silentSetup_withAForeignStatusLine_printsTheOneLineToAddAndChangesNothing(self):
        original = {'statusLine': {'type': 'command', 'command': 'bash my-line.sh'}}
        self.settings.write_text(json.dumps(original), encoding='utf-8')
        out = pacing.cmd_setup(str(self.cfg_dir), pacing.__file__, silent=True)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), original)
        self.assertIn('statusline --record-only', out)

    def test_noSettingsFile_createsStatusLineAndStableCopy(self):
        out = self._run()
        conf = json.loads(self.settings.read_text(encoding='utf-8'))
        stable = self.cfg_dir / 'pace' / 'pacing.py'
        self.assertTrue(stable.exists())
        self.assertEqual(conf['statusLine']['type'], 'command')
        self.assertIn('statusline --full --standalone', conf['statusLine']['command'])
        self.assertIn(stable.as_posix(), conf['statusLine']['command'])
        self.assertIn('/pace on', out)

    def test_keepsEveryOtherKeyAndBacksUp(self):
        original = {'permissions': {'allow': ['Bash(ls)']}, 'hooks': {'Stop': []}, 'model': 'opus'}
        self.settings.write_text(json.dumps(original), encoding='utf-8')
        self._run()
        conf = json.loads(self.settings.read_text(encoding='utf-8'))
        self.assertEqual({k: v for k, v in conf.items() if k != 'statusLine'}, original)
        backups = list(self.cfg_dir.glob('settings.json.pace-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8')), original)

    def test_existingForeignStatusLine_isKeptAndSnippetPrinted(self):
        original = {'statusLine': {'type': 'command', 'command': 'bash my-line.sh'}}
        self.settings.write_text(json.dumps(original), encoding='utf-8')
        out = self._run()
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), original)
        self.assertIn('statusline', out)
        self.assertIn('kept', out.lower())

    def test_paceStatusLine_isIdempotent(self):
        self._run()
        first = self.settings.read_text(encoding='utf-8')
        out = self._run()
        self.assertEqual(self.settings.read_text(encoding='utf-8'), first)
        self.assertIn('already', out.lower())

    def test_invalidSettings_isNeverRewritten(self):
        self.settings.write_text('{ not json', encoding='utf-8')
        out = self._run()
        self.assertEqual(self.settings.read_text(encoding='utf-8'), '{ not json')
        self.assertIn('not valid JSON', out)

    def test_ownInstall_usesTheHooksCopyWithoutDuplicating(self):
        (self.cfg_dir / 'hooks').mkdir()
        own = self.cfg_dir / 'hooks' / 'pacing.py'
        own.write_text('# stub', encoding='utf-8')
        pacing.cmd_setup(str(self.cfg_dir), str(own))
        conf = json.loads(self.settings.read_text(encoding='utf-8'))
        self.assertIn(own.as_posix(), conf['statusLine']['command'])
        self.assertFalse((self.cfg_dir / 'pace').exists())

    def test_standaloneFull_withoutTemplate_printsAnL1LayoutNoMarker(self):
        raw = {'session_id': 's', 'model': {'display_name': 'Claude Opus'},
               'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': 1791648600},
                               'seven_day': {'used_percentage': 71, 'resets_at': RESET7}}}
        with mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': str(self.cfg_dir / 'state')}):
            out = pacing.cmd_statusline_full(raw, CFG, NOW, '', default_template='L1')
        self.assertNotIn(pacing.SEGMENT_MARK, out)
        self.assertIn('Opus', out)

    def test_failedWrite_isReportedNotClaimed(self):
        original = {'model': 'x'}
        self.settings.write_text(json.dumps(original), encoding='utf-8')
        with mock.patch.object(pacing.os, 'replace', side_effect=PermissionError('locked')):
            out = self._run()
        self.assertIn('could not write', out)
        self.assertNotIn('statusLine set', out)
        self.assertEqual(json.loads(self.settings.read_text(encoding='utf-8')), original)
        self.assertEqual(list(self.cfg_dir.glob('settings.json.pace-backup-*')), [])

    def test_settingsFileMode_isRestoredWithAnIntegerOnPosix(self):
        (self.cfg_dir / 'settings.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(pacing.os, 'name', 'posix'), mock.patch.object(pacing.os, 'chmod') as chmod:
            pacing.cmd_setup(str(self.cfg_dir), pacing.__file__)
        self.assertTrue(chmod.called)
        for call in chmod.call_args_list:  # one call restores settings.json, another may protect a temp file
            self.assertIsInstance(call.args[1], int)

    def test_unwritableDir_returnsAReadableLineInsteadOfATraceback(self):
        with mock.patch.object(pacing.shutil, 'copyfile', side_effect=OSError('disk full')):
            out = self._run()
        self.assertIn('setup failed', out)

    def test_bomSettings_isAccepted(self):
        self.settings.write_bytes(b'\xef\xbb\xbf' + json.dumps({'model': 'x'}).encode())
        self._run()
        conf = json.loads(self.settings.read_text(encoding='utf-8'))
        self.assertEqual((conf['model'], conf['statusLine']['type']), ('x', 'command'))

    def test_nonObjectSettings_saysNotAnObject(self):
        self.settings.write_text('[]', encoding='utf-8')
        self.assertIn('not a JSON object', self._run())
        self.assertEqual(self.settings.read_text(encoding='utf-8'), '[]')

    def test_staleOwnLine_isRepointedWithBackup(self):
        stale = {'statusLine': {'type': 'command', 'command':
                                'python -S "/h/.claude/plugins/cache/pace/1.0/scripts/pacing.py" statusline --full'}}
        self.settings.write_text(json.dumps(stale), encoding='utf-8')
        self._run()
        cmd = json.loads(self.settings.read_text(encoding='utf-8'))['statusLine']['command']
        self.assertIn((self.cfg_dir / 'pace' / 'pacing.py').as_posix(), cmd)
        self.assertEqual(len(list(self.cfg_dir.glob('settings.json.pace-backup-*'))), 1)

    def test_promptHook_blocksWithTheSetupText(self):
        env = {'CLAUDE_CONFIG_DIR': str(self.cfg_dir), 'CLAUDE_PACING_DIR': str(self.cfg_dir / 'state')}
        with mock.patch.dict(os.environ, env):
            out = pacing.cmd_prompt({'prompt': '/pace setup'}, CFG, NOW)
        self.assertEqual(out['decision'], 'block')
        self.assertIn('pace setup:', out['reason'])
        self.assertTrue((self.cfg_dir / 'settings.json').exists())

    def test_configDir_followsTheEnvironment(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.cfg_dir)}):
            self.assertEqual(pacing.config_dir(), str(self.cfg_dir))

    def test_refresh_replacesAStaleStableCopyOnly(self):
        self._run()
        stable = self.cfg_dir / 'pace' / 'pacing.py'
        stable.write_text('# old version', encoding='utf-8')
        pacing.refresh_stable_copy(str(self.cfg_dir), pacing.__file__)
        self.assertEqual(stable.read_bytes(), Path(pacing.__file__).read_bytes())
        other = self.cfg_dir / 'elsewhere'
        pacing.refresh_stable_copy(str(other), pacing.__file__)  # setup never ran there: nothing is created
        self.assertFalse(other.exists())

    def test_naStatus_explainsWhyThereIsNoData(self):
        status = pacing.format_status({'state': 'na'}, {}, CFG, NOW)
        brief = pacing.format_brief({'state': 'na'}, {'wk': {'measure': True}}, CFG, NOW)
        for text in (status, brief):
            self.assertIn('2.1.80', text)
            self.assertIn('/pace setup', text)


class SharedState(unittest.TestCase):
    """Retention seeds the u5 envelope; a changed step recomputes the cached decision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_retention_keepsASeedOlderThanTheLongestWindow(self):
        r5 = _sample(0, 0)['r5']
        for sid, ts, u5 in (('a', NOW - 2000, 50), ('b', NOW - 1790, 40), ('a', NOW - 600, 50), ('a', NOW, 50)):
            pacing.record_sample(self.sd, sid, ts, u5, 71, r5, RESET7, CFG)  # b = idle session, stale reading
        dec = pacing.decide(NOW, pacing.load_samples(self.sd, NOW, CFG), CFG)
        self.assertEqual(set(dec['trend'].values()), {0})

    def test_changedStep_recomputesTheCachedDecision(self):
        raw = {'session_id': 's', 'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': 1791648600},
                                                  'seven_day': {'used_percentage': 71, 'resets_at': RESET7}}}
        with mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': self.sd}):
            Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
            first, _, _ = pacing._statusline_state(raw, CFG, NOW)
            Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG) + 1}))
            second, _, _ = pacing._statusline_state(raw, CFG, NOW + 1)
        self.assertEqual((first['mult'], second['mult']), (1.0, CFG['steps'][pacing.default_step(CFG) + 1]))


class WindowRollover(unittest.TestCase):
    """A 5h or 7d window that already reset must not keep driving the delay, the cache or the status line."""
    T0 = 1_791_640_000.0
    BOTH = ('wk', 'h5')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = self.tmp.name
        self.env = mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': self.sd})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _raw(self, r5, u5=100, u7=71, r7=RESET7):
        return {'session_id': 's', 'rate_limits': {'five_hour': {'used_percentage': u5, 'resets_at': r5},
                                                  'seven_day': {'used_percentage': u7, 'resets_at': r7}}}

    def test_expiredFiveHour_isNotLevel3AndHasNoFiveHourDelay(self):
        dec = pacing.decide(NOW, [_sample(NOW, 100, 71, r5=NOW - 485)], CFG, axes=self.BOTH)
        self.assertNotEqual(dec['level'], 3)
        self.assertEqual(dec['delay_5h'], 0)
        self.assertIsNone(dec['u5'])
        self.assertIsNone(dec['r5'])

    def test_expiredFiveHour_keepsTheWeeklyLevelAndLead(self):
        old = NOW - 485
        pts = [_sample(NOW - 900, 60, 71, r5=old), _sample(NOW - 300, 90, 71, r5=old), _sample(NOW, 100, 71, r5=old)]
        dec = pacing.decide(NOW, pts, CFG, axes=self.BOTH)
        self.assertEqual(dec['state'], 'L1')
        self.assertGreater(dec['lead'], CFG['deadband_pct'])
        self.assertEqual(dec['delay_5h'], 0)

    def test_newWindowSample_winsOverTheExpiredOne(self):
        pts = [_sample(NOW - 5, 100, 71, r5=NOW - 485), _sample(NOW, 3, 71, r5=NOW + 17000)]
        dec = pacing.decide(NOW, pts, CFG, axes=self.BOTH)
        self.assertEqual((dec['u5'], dec['r5']), (3, NOW + 17000))
        self.assertNotEqual(dec['level'], 3)

    def test_expiredWeek_isStaleWithNoDelay(self):
        pt = {'ts': NOW, 'u5': 40, 'u7': 71, 'r5': NOW + 3600, 'r7': NOW - 10}
        dec = pacing.decide(NOW, [pt], CFG, axes=self.BOTH)
        self.assertEqual((dec['state'], dec['delay_rec']), ('stale', 0))

    def test_cachedDecision_isRecomputedWhenTheWindowChanged(self):
        Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
        first, _, _ = pacing._statusline_state(self._raw(NOW - 1), CFG, NOW - 5)
        second, _, _ = pacing._statusline_state(self._raw(NOW + 17000, u5=3), CFG, NOW)
        self.assertEqual((first['r5'], second['r5']), (NOW - 1, NOW + 17000))

    def test_cachedDecision_isKeptWhileTheWindowIsTheSame(self):
        Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
        first, _, _ = pacing._statusline_state(self._raw(NOW + 3600, u5=40), CFG, NOW - 5)
        second, _, _ = pacing._statusline_state(self._raw(NOW + 3600, u5=41), CFG, NOW)
        self.assertEqual(second['ts'], first['ts'])

    def _decision(self, **kw):
        base = {'ts': self.T0, 'state': 'L3', 'level': 3, 'delay_rec': 45.0, 'delay_wk': 45.0, 'delay_5h': 45.0}
        Path(self.sd, 'decision.json').write_text(json.dumps({**base, **kw}))
        Path(self.sd, 'ctl.json').write_text(json.dumps({'on': True, 'step': pacing.default_step(CFG)}))

    def _slept(self, now):
        slept = []
        pacing.cmd_enforce({'tool_name': 'Edit', 'session_id': 's1'}, dict(CFG), now, sleep=slept.append)
        return sum(slept)

    def test_enforce_expiredWindowDecision_doesNotSleep(self):
        self._decision(r5=self.T0 - 5)
        self.assertEqual(self._slept(self.T0 + 1), 0)

    def test_enforce_liveWindowDecision_sleeps(self):
        self._decision(r5=self.T0 + 3600)
        self.assertEqual(self._slept(self.T0 + 1), 45)

    def test_enforce_expiredWindowBelowLevel3_keepsOnlyTheWeeklyDelay(self):
        self._decision(r5=self.T0 - 5, state='L1', level=1, delay_wk=20.0, delay_5h=40.0, delay_rec=40.0)
        Path(self.sd, 'ctl.json').write_text(json.dumps({'on': True, 'h5': {'on': True},
                                                         'step': pacing.default_step(CFG)}))
        self.assertEqual(self._slept(self.T0 + 1), 20)

    def test_enforce_expiredLevel3Decision_doesNotDenySubagents(self):
        now = time.time()
        self._decision(ts=now, r5=now - 5)
        for _ in range(CFG['n_max']):
            self.assertTrue(pacing.claim_agent(self.sd, now, CFG, False))
        out = pacing.cmd_enforce({'tool_name': 'Agent', 'session_id': 's1'}, dict(CFG), now, sleep=lambda s: None)
        self.assertIsNone(out)

    def test_cachedDecision_keptWhenAnIdleSessionReportsAnExpiredWindow(self):
        Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
        first, _, _ = pacing._statusline_state(self._raw(NOW - 1), CFG, NOW + 5)  # window gone: r5 -> None
        second, _, _ = pacing._statusline_state(self._raw(NOW - 1), CFG, NOW + 8)
        self.assertEqual((first['r5'], second['ts']), (None, first['ts']))

    def test_cachedDecision_recomputesOnceAfterItsWindowExpires(self):
        Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
        first, _, _ = pacing._statusline_state(self._raw(NOW + 10, u5=40), CFG, NOW)
        second, _, _ = pacing._statusline_state(self._raw(NOW + 10, u5=40), CFG, NOW + 12)
        third, _, _ = pacing._statusline_state(self._raw(NOW + 10, u5=40), CFG, NOW + 15)
        self.assertEqual((first['r5'], second['r5'], third['ts']), (NOW + 10, None, second['ts']))

    def test_cachedDecision_recomputesWhenTheWeekTurns(self):
        Path(self.sd, 'ctl.json').write_text(json.dumps({'wk': {'on': True}, 'step': pacing.default_step(CFG)}))
        first, _, _ = pacing._statusline_state(self._raw(NOW + 3600, r7=NOW + 10), CFG, NOW)
        second, _, _ = pacing._statusline_state(self._raw(NOW + 3600, r7=NOW + 10), CFG, NOW + 12)
        self.assertEqual((first['state'] != 'stale', second['state']), (True, 'stale'))

    def test_sampleDedupe_recordsANewWindowEvenWhenUsageIsUnchanged(self):
        pacing.record_sample(self.sd, 's', NOW, 3, 71, NOW + 100, RESET7, CFG)
        pacing.record_sample(self.sd, 's', NOW + 10, 3, 71, NOW + 18100, RESET7, CFG)
        rows = pacing.load_samples(self.sd, NOW + 10, CFG)
        self.assertEqual([r['r5'] for r in rows], [NOW + 100, NOW + 18100])

    def test_view_expiredWindows_showAsZeroWithNoCountdown(self):
        v = pacing.view_from_raw(self._raw(NOW - 60, u5=100, r7=NOW - 1), '', NOW)
        self.assertEqual((v['five'], v['week']), ((0.0, 0), (0.0, 0)))

    def test_view_liveWindow_isUnchanged(self):
        v = pacing.view_from_raw(self._raw(NOW + 600, u5=63), '', NOW)
        self.assertEqual((v['five'], v['week']), ((63, 600), (71, RESET7 - NOW)))

    def test_templates_drawTheResetWindowAsEmpty(self):
        view = pacing.view_from_raw(self._raw(NOW - 60, u5=100), '', NOW)
        out = pacing.render_template('L1', view, {'state': 'ok', 'level': 0}, {}, CFG, NOW)
        self.assertIn('5h ░░░░░░░░░░ 0%', out)
        self.assertNotIn('100%', out)


class Guide(unittest.TestCase):
    """`pace guide` finds the manual that ships with the script and opens it without a browser in tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CLAUDE_PACING_DIR=self.tmp.name, CLAUDE_PACING_NO_OPEN='1',
                        CLAUDE_PACING_CONFIG=str(Path(self.tmp.name) / 'none.json'))

    def tearDown(self): self.tmp.cleanup()

    def test_parse_acceptsGuide(self):
        self.assertEqual(pacing.parse_pace_prompt('/pace guide'), 'guide')
        self.assertEqual(pacing.parse_pace_prompt('pace  GUIDE'), 'guide')

    def test_findGuide_resolvesTheManualBesideTheScriptRoot(self):
        found = pacing.find_guide(str(HOOKS / 'pacing.py'))
        self.assertEqual(Path(found), (HOOKS.parent / 'docs' / 'pacing-cadencer-readme.html').resolve())

    def test_cmdGuide_printsPathAndUriWithoutOpeningWhenDisabled(self):
        with mock.patch.dict(os.environ, {'CLAUDE_PACING_NO_OPEN': '1'}), \
                mock.patch.object(pacing, '_open_in_browser') as opener:
            out = pacing.cmd_guide(str(HOOKS / 'pacing.py'))
        opener.assert_not_called()
        self.assertIn('pacing-cadencer-readme.html', out)
        self.assertIn('file:///', out)
        self.assertIn('Open the link above', out)

    def test_cmdGuide_opensTheBrowserByDefault(self):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PACING_NO_OPEN'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pacing, '_open_in_browser', return_value=True) as opener:
            out = pacing.cmd_guide(str(HOOKS / 'pacing.py'))
        self.assertTrue(opener.call_args[0][0].endswith('pacing-cadencer-readme.html'))
        self.assertIn('file:///', out)  # the link is always printed, whether or not the opener worked
        self.assertIn('Asked the desktop', out)

    def test_openInBrowser_noDisplay_doesNotSpawnAnything(self):
        env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(pacing.sys, 'platform', 'linux'), \
                mock.patch('subprocess.Popen') as popen:
            self.assertFalse(pacing._open_in_browser('/x/manual.html'))
        popen.assert_not_called()

    def test_openInBrowser_withDisplay_detachesFromTheHookStreams(self):
        with mock.patch.dict(os.environ, {'DISPLAY': ':0'}), mock.patch.object(pacing.sys, 'platform', 'linux'), \
                mock.patch.object(pacing.shutil, 'which', return_value='/usr/bin/xdg-open'), \
                mock.patch('subprocess.Popen') as popen:
            self.assertTrue(pacing._open_in_browser('/x/manual.html'))
        kw = popen.call_args.kwargs
        self.assertEqual((kw['stdin'], kw['stdout'], kw['stderr']), (subprocess.DEVNULL,) * 3)
        self.assertTrue(kw['start_new_session'])

    def test_openInBrowser_macos_passesAFileUriSoTheDefaultBrowserOpensIt(self):
        manual = str(Path(self.tmp.name) / 'manual.html')
        with mock.patch.object(pacing.sys, 'platform', 'darwin'), \
                mock.patch.object(pacing.shutil, 'which', return_value='/usr/bin/open'), \
                mock.patch('subprocess.Popen') as popen:
            self.assertTrue(pacing._open_in_browser(manual))
        self.assertEqual(popen.call_args.args[0], ['open', Path(manual).as_uri()])

    def test_ctlFailure_isFailOpenSoTheChainDoesNotReRunIt(self):
        env = {'CLAUDE_PACING_DIR': self.tmp.name, 'CLAUDE_PACING_CONFIG': self.env['CLAUDE_PACING_CONFIG']}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(pacing, 'format_status', side_effect=RuntimeError('boom')):
            code = pacing.main(['pacing.py', 'ctl', 'up'])
        self.assertEqual(code, 0)

    def test_badConfigValue_doesNotCrashTheHook(self):
        cfgfile = Path(self.tmp.name) / 'bad.json'
        cfgfile.write_text(json.dumps({'cap_s': 'forty-five'}), encoding='utf-8')
        env = dict(self.env, CLAUDE_PACING_CONFIG=str(cfgfile))
        r = subprocess.run([sys.executable, '-S', str(HOOKS / 'pacing.py'), 'ctl', 'status'],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('config', (Path(self.tmp.name) / 'pacing.err').read_text(encoding='utf-8'))

    def test_cmdGuide_uriEncodesSpacesAndAccents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'Pace Usér'
            (root / 'scripts').mkdir(parents=True)
            (root / 'docs').mkdir()
            (root / 'docs' / 'pacing-cadencer-readme.html').write_text('<html></html>', encoding='utf-8')
            with mock.patch.dict(os.environ, {'CLAUDE_PACING_NO_OPEN': '1'}):
                out = pacing.cmd_guide(str(root / 'scripts' / 'pacing.py'))
        self.assertIn('Pace%20Us%C3%A9r', out)

    def test_cmdGuide_missingManualSaysSo(self):
        with tempfile.TemporaryDirectory() as d:
            out = pacing.cmd_guide(os.path.join(d, 'pacing.py'))
        self.assertIn('was not found', out)

    def test_ctlGuide_printsThePathAndExitsZero(self):
        r = subprocess.run([sys.executable, '-S', str(HOOKS / 'pacing.py'), 'ctl', 'guide'],
                           capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn('pacing-cadencer-readme.html', r.stdout)

    def test_promptGuide_blocksWithThePath(self):
        r = subprocess.run([sys.executable, '-S', str(HOOKS / 'pacing.py'), 'prompt'], input=json.dumps({'prompt': 'pace guide'}),
                           capture_output=True, text=True, env=self.env, timeout=30)
        out = json.loads(r.stdout)
        self.assertEqual(out['decision'], 'block')
        self.assertIn('pacing-cadencer-readme.html', out['reason'])


class Versioning(unittest.TestCase):
    """pace carries its own MAJOR.MINOR version (not the roadmap phase); the manual shows the same one."""

    def test_versionIsMajorDotMinorCounter(self):
        self.assertRegex(pacing.VERSION, r'^\d+\.\d+$')

    def test_manualBrandTagMatchesTheCode(self):
        html = (HOOKS.parent / 'docs' / 'pacing-cadencer-readme.html').read_text(encoding='utf-8')
        self.assertIn(f'<span class="brand__ver">v{pacing.VERSION}</span>', html)

    def test_statusReportsTheVersion(self):
        out = pacing.format_status({'state': 'ok', 'level': 0}, {}, CFG, NOW)
        self.assertIn(f'pace v{pacing.VERSION}', out)


class CacheBudget(unittest.TestCase):
    """Pacing must never push the gap between two model requests past the prompt-cache TTL."""
    T0 = 1_791_640_000.0  # end of the last model response

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'state').mkdir()
        self.env = mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': str(self.root / 'state')})
        self.env.start()
        (self.root / 'state' / 'ctl.json').write_text(json.dumps({'on': True, 'step': pacing.default_step(CFG)}))
        self.cfg = dict(CFG)
        self.ceiling = self.cfg['cache_ttl_s'] - self.cfg['cache_margin_s']

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _decision(self, delay=45.0):
        (self.root / 'state' / 'decision.json').write_text(json.dumps(
            {'ts': self.T0, 'state': 'L1', 'level': 1, 'delay_rec': delay}))

    def _transcript(self, path, ts):
        iso = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps({'type': 'user', 'timestamp': iso}), json.dumps({'type': 'assistant', 'timestamp': iso})]
        Path(path).write_text(chr(10).join(rows) + chr(10))
        return str(path)

    def _call(self, now, **raw):
        slept = []
        base = {'tool_name': 'Edit', 'session_id': 's1'}
        pacing.cmd_enforce({**base, **raw}, self.cfg, now, sleep=slept.append)
        return sum(slept)

    def test_serialToolsOfOneTurn_neverAddMoreThanCapInTotal(self):
        self._decision()
        tr = self._transcript(self.root / 'p' / 's1.jsonl', self.T0)
        for exec_s, n in ((2, 6), (10, 6), (20, 6), (30, 8)):
            sid, now, total = f'chain{exec_s}', self.T0, 0.0
            for _ in range(n):
                s = self._call(now, session_id=sid, transcript_path=tr)
                total += s
                now += s + exec_s
            self.assertLessEqual(total, self.cfg['cap_s'], (exec_s, n, total))

    def test_sleepNeverCrossesTheCeilingSinceLastResponse(self):
        self._decision()
        tr = self._transcript(self.root / 'p' / 's1.jsonl', self.T0)
        self.assertEqual(self._call(self.T0 + self.ceiling - 10, transcript_path=tr, session_id='a'), 10)
        self.assertEqual(self._call(self.T0 + self.ceiling + 5, transcript_path=tr, session_id='b'), 0)

    def test_newTurnGetsAFreshBudget(self):
        self._decision()
        tr = self._transcript(self.root / 'p' / 's1.jsonl', self.T0)
        self.assertEqual(self._call(self.T0 + 1, transcript_path=tr), 45)
        self.assertEqual(self._call(self.T0 + 50, transcript_path=tr), 0)  # same turn: budget used
        self._transcript(self.root / 'p' / 's1.jsonl', self.T0 + 60)  # model answered again
        self.assertEqual(self._call(self.T0 + 61, transcript_path=tr), 45)

    def test_unknownTurn_capsOneSleepPerCeilingWindow(self):
        self._decision()
        self.assertEqual(self._call(self.T0), 45)
        self.assertEqual(self._call(self.T0 + 60), 0)
        self.assertEqual(self._call(self.T0 + self.ceiling + 1), 0)  # still inside the TTL window
        self.assertEqual(self._call(self.T0 + self.cfg['cache_ttl_s'] + 1), 45)

    def test_subagentUsesItsOwnTranscript_notTheStaleParentOne(self):
        self._decision()
        main = self._transcript(self.root / 'p' / 's1.jsonl', self.T0 - 3600)  # parent idle for an hour
        self._transcript(self.root / 'p' / 's1' / 'subagents' / 'agent-ag1.jsonl', self.T0)
        self.assertEqual(self._call(self.T0 + 1, transcript_path=main, agent_id='ag1'), 45)

    def test_withoutAgentId_usesNewestTranscriptOfTheSession(self):
        self._decision()
        main = self._transcript(self.root / 'p' / 's1.jsonl', self.T0 - 3600)
        sub = self._transcript(self.root / 'p' / 's1' / 'subagents' / 'agent-zz.jsonl', self.T0)
        os.utime(main, (self.T0 - 3600, self.T0 - 3600))
        os.utime(sub, (self.T0, self.T0))
        self.assertEqual(self._call(self.T0 + 1, transcript_path=main), 45)

    def test_configCannotRaiseCapAboveTheCacheOrHookLimit(self):
        cfgfile = self.root / 'cfg.json'
        with mock.patch.dict(os.environ, {'CLAUDE_PACING_CONFIG': str(cfgfile)}):
            cfgfile.write_text(json.dumps({'cap_s': 600}))
            self.assertEqual(pacing.load_config()['cap_s'], pacing.MAX_CAP_S)
            cfgfile.write_text(json.dumps({'cap_s': 600, 'cache_ttl_s': 100, 'cache_margin_s': 90}))
            self.assertEqual(pacing.load_config()['cap_s'], 10)


class Brief(unittest.TestCase):
    """`/pace brief`: a header and one line per observed axis, narrow enough for a phone."""

    def _dec(self, **kw):
        base = {'state': 'L1', 'level': 1, 'lead': -25.0, 'u5': 24, 'u7': 45, 'r5': NOW + 6600, 'r7': NOW + 90000,
                'now_rate': 1.76, 'need_rate': 1.59, 'reduce_pct': 10, 'delay_rec': 5.06, 'delay_wk': 0.0,
                'delay_5h': 5.06, 'now_rate5': 17.6, 'need_rate5': 21.09, 'deficit5': 0.56, 'reduce5_pct': 0,
                'allow_day': 38.0}
        base.update(kw)
        return base

    def _ctl(self, wk=(False, True), h5=(True, False), extra=0):
        return {'wk': {'on': wk[0], 'measure': wk[1]}, 'h5': {'on': h5[0], 'measure': h5[1]},
                'step': pacing.default_step(CFG) + extra}

    def _brief(self, dec=None, ctl=None):
        return pacing.format_brief(dec or self._dec(), self._ctl() if ctl is None else ctl, CFG, NOW).splitlines()

    def test_h5Acting_headerAndOneLinePerObservedAxis(self):
        self.assertEqual(self._brief(), [
            'pace [L1] · auto x1 · delay 5s · r:5h',
            '5h    24% used · 18 of 21 %/h · resets 1h 50m',
            'week  45% used · 1.8 of 1.6 %/h · cut 10% · 25% under budget'])

    def test_manualStepIsNamedInTheHeader(self):
        self.assertIn('manual x1.5', self._brief(ctl=self._ctl(extra=1))[0])

    def test_overBudgetAndOnBudgetWording(self):
        over = self._brief(self._dec(lead=8.0, reduce_pct=0))
        self.assertTrue(over[2].endswith('8% over budget'), over)
        flat = self._brief(self._dec(lead=0.5, reduce_pct=0))
        self.assertTrue(flat[2].endswith('on budget'), flat)

    def test_nothingEngaged_isTheHeaderOnly(self):
        out = self._brief(self._dec(state='ok', level=0, lead=0.0), {})
        self.assertEqual(out, ['pace [off] · auto x1'])

    def test_noSample_saysSoInsteadOfInventingNumbers(self):
        out = self._brief({'state': 'na'}, self._ctl())
        self.assertEqual(out, ['pace [n/a] · auto x1 · r:5h', 'no usage sample yet', pacing.NO_DATA_HINT])

    def test_axisWithBothOffIsLeftOut(self):
        out = self._brief(ctl=self._ctl(wk=(False, False)))
        self.assertEqual([x.split()[0] for x in out], ['pace', '5h'])

    def test_noLineExceedsAPhoneWidth(self):
        for line in self._brief():
            self.assertLessEqual(len(line), 60, line)

    def test_promptAcceptsBriefAndItIsNotAStateChange(self):
        self.assertEqual(pacing.parse_pace_prompt('/pace brief'), 'brief')
        self.assertEqual(pacing.parse_pace_prompt('pace  BRIEF'), 'brief')
        c = {'step': 3}
        self.assertEqual(pacing.apply_ctl('brief', c, CFG)['step'], 3)


class SummaryLine(unittest.TestCase):
    """`template after`: one line, same vocabulary as the `cadence:` segment, printed after the user's lines."""
    _dec = Brief._dec
    _ctl = Brief._ctl

    def _line(self, dec=None, ctl=None):
        return pacing.format_line(dec or self._dec(), self._ctl() if ctl is None else ctl, CFG, NOW)

    def test_h5ActingWeekMeasuring_showsBothBlocks(self):
        self.assertEqual(self._line(), 'pace [L1] 5h:24% 18>21%/h reset 1h50m | wk:45% 2%/h -25%')

    def test_behindOnBoth_showsCutsAndTheWeeklyDeviation(self):
        dec = self._dec(lead=8.0, deficit5=2.0, now_rate5=44.0, need_rate5=18.0, reduce5_pct=59, u5=31, u7=41,
                        now_rate=4.0, need_rate=3.0)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))),
                         'pace [L1] 5h:31% 44>18%/h cut 59% reset 1h50m | wk:41% 4>3%/h cut 10% +8%')

    def test_leadInsideTheDeadband_isLeftOut(self):
        dec = self._dec(lead=-2.0, deficit5=2.0, now_rate5=44.0, need_rate5=18.0, reduce5_pct=59, u5=31, u7=41,
                        now_rate=4.2, reduce_pct=0)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))),
                         'pace [L1] 5h:31% 44>18%/h cut 59% reset 1h50m | wk:41% 4%/h')

    def test_level3_hasNoRateAndNoCut(self):
        dec = self._dec(state='L3', level=3, u5=97, r5=NOW + 720)
        self.assertEqual(self._line(dec, self._ctl(wk=(True, False))), 'pace [L3] 5h:97% reset 12m | wk:45% -25%')

    def test_axisWithBothOffIsLeftOut(self):
        self.assertEqual(self._line(ctl=self._ctl(wk=(False, False))), 'pace [L1] 5h:24% 18>21%/h reset 1h50m')

    def test_nothingEngaged_isTheTagOnly(self):
        self.assertEqual(self._line(self._dec(state='ok', level=0, lead=0.0), {}), 'pace [off]')

    def test_noSample_saysSoWithTheSegmentTag(self):
        self.assertEqual(self._line({'state': 'na'}, self._ctl()), 'pace [n/a] no usage sample yet')
        self.assertEqual(self._line({'state': 'na'}, {}), 'pace [off] no usage sample yet')

    def test_partialDecision_neverRaises(self):
        self.assertEqual(self._line({'state': 'L1'}, self._ctl()), 'pace [off]')

    def test_isOneLine(self):
        self.assertNotIn('\n', self._line())


class AfterContract(unittest.TestCase):
    """`statusline --full --after`: the caller says it understands APPEND_MARK; without it nothing changes."""
    RAW = {'session_id': 's', 'model': {'display_name': 'Claude Opus'},
           'rate_limits': {'five_hour': {'used_percentage': 40, 'resets_at': 1791648600},
                           'seven_day': {'used_percentage': 71, 'resets_at': RESET7}}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sd = Path(self.tmp.name) / 'state'
        self.sd.mkdir()
        self.env = mock.patch.dict(os.environ, {'CLAUDE_PACING_DIR': str(self.sd)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _full(self, template, after, default=''):
        ctl = {'wk': {'on': True}}
        if template:
            ctl['template'] = template
        (self.sd / 'ctl.json').write_text(json.dumps(ctl), encoding='utf-8')
        return pacing.cmd_statusline_full(self.RAW, CFG, NOW, '', default, after)

    def test_afterWithTheFlag_isTheAppendMarkAndOneLine(self):
        out = self._full('AFTER', True)
        self.assertTrue(out.startswith(pacing.APPEND_MARK + 'pace ['), out)
        self.assertNotIn('\n', out)

    def test_afterWithoutTheFlag_isTheOldSegment(self):
        out = self._full('AFTER', False)
        self.assertTrue(out.startswith(pacing.SEGMENT_MARK + 'cadence:'), out)

    def test_noTemplateWithTheFlag_isTheOldSegment(self):
        self.assertTrue(self._full('', True).startswith(pacing.SEGMENT_MARK + 'cadence:'))

    def test_aFullLayoutWinsOverTheFlag(self):
        out = self._full('L2', True)
        self.assertNotIn(pacing.APPEND_MARK, out)
        self.assertNotIn(pacing.SEGMENT_MARK, out)

    def test_standalone_ignoresAfter(self):
        out = self._full('AFTER', True, default='L1')
        self.assertNotIn(pacing.APPEND_MARK, out)
        self.assertNotIn(pacing.SEGMENT_MARK, out)
        self.assertIn('Opus', out)


class Cli(unittest.TestCase):
    """Process-level checks: default OFF, fail-open, zero output when idle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfgdir = tempfile.TemporaryDirectory()
        cfgfile = Path(self.cfgdir.name) / 'pacing.config.json'
        cfgfile.write_text(json.dumps({'exempt_agents': EXEMPT}), encoding='utf-8')
        self.env = dict(os.environ, CLAUDE_PACING_DIR=self.tmp.name, CLAUDE_PACING_CONFIG=str(cfgfile))

    def tearDown(self):
        self.tmp.cleanup()
        self.cfgdir.cleanup()

    def _run(self, sub, payload=None, *args):
        r = subprocess.run([sys.executable, '-S', str(HOOKS / 'pacing.py'), sub, *args],
                           input=json.dumps(payload or {}), capture_output=True, text=True, env=self.env, timeout=30)
        return r.returncode, r.stdout.strip()

    def _status(self, now=None):
        now = now or time.time()
        return {'session_id': 's1', 'rate_limits': {'five_hour': {'used_percentage': 37, 'resets_at': int(now) + 3600},
                                                    'seven_day': {'used_percentage': 71, 'resets_at': int(now) + 4 * 86400}}}

    def test_enforce_defaultOff_isSilentAndFast(self):
        t = time.monotonic()
        code, out = self._run('enforce', {'tool_name': 'Bash', 'session_id': 's1'})
        self.assertEqual((code, out), (0, ''))
        self.assertLess(time.monotonic() - t, 2)

    def test_statusline_writesSampleAndShowsOff(self):
        code, out = self._run('statusline', self._status())
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('cadence: [off]'), out)
        self.assertTrue(list(Path(self.tmp.name).glob('samples.s1.jsonl')))

    def test_statusline_recordOnly_printsNothingButSamplesAndDecides(self):
        code, out = self._run('statusline', self._status(), '--record-only')
        self.assertEqual((code, out), (0, ''))
        self.assertTrue(list(Path(self.tmp.name).glob('samples.s1.jsonl')))
        self.assertTrue((Path(self.tmp.name) / 'decision.json').exists())

    def test_statuslineFullAfter_printsTheAppendMark(self):
        (Path(self.tmp.name) / 'ctl.json').write_text(json.dumps({'template': 'AFTER', 'wk': {'on': True}}),
                                                      encoding='utf-8')
        code, out = self._run('statusline', self._status(), '--full', '--after')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('\x02pace ['), repr(out))
        self.assertNotIn('\n', out)

    def test_statusline_withoutRateLimits_failsOpen(self):
        code, out = self._run('statusline', {'session_id': 's1'})
        self.assertEqual((code, out), (0, 'cadence: [off]'))

    def test_promptCommand_blocksAndPersistsOn(self):
        code, out = self._run('prompt', {'prompt': '/pace on'})
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body['decision'], 'block')
        self.assertIn('on', body['reason'])
        ctl = json.loads((Path(self.tmp.name) / 'ctl.json').read_text())
        self.assertEqual((ctl['wk']['on'], ctl['h5']['on']), (True, True))

    def test_axisCommand_persistsOnlyThatAxis(self):
        self._run('prompt', {'prompt': '/pace 5h on'})
        ctl = json.loads((Path(self.tmp.name) / 'ctl.json').read_text())
        self.assertEqual((ctl['h5']['on'], ctl['wk']['on']), (True, False))

    def test_measureMode_showsROffAndNeverSleeps(self):
        body = json.loads(self._run('prompt', {'prompt': 'pace measure'})[1])
        self.assertEqual(body['decision'], 'block')
        ctl = json.loads((Path(self.tmp.name) / 'ctl.json').read_text())
        self.assertEqual((ctl['wk']['measure'], ctl['h5']['measure'], ctl['wk']['on']), (True, True, False))
        out = self._run('statusline', self._status())[1]
        self.assertTrue(out.endswith('r:off'), out)
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'delay_rec': 5}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        t = time.monotonic()
        self.assertEqual(self._run('enforce', {'tool_name': 'Bash', 'session_id': 's1'}), (0, ''))
        self.assertLess(time.monotonic() - t, 3)

    def test_upEntersManual_autoReturnsToSuggestedCadence(self):
        self._run('prompt', {'prompt': 'pace 5h on'})
        up = json.loads(self._run('prompt', {'prompt': 'pace up'})[1])['reason']
        self.assertIn('mode manual', up)
        self.assertIn('step x1.5', up)
        down = json.loads(self._run('prompt', {'prompt': 'pace down'})[1])['reason']
        self.assertIn('mode auto', down)  # back on the suggested step by hand
        self._run('prompt', {'prompt': 'pace down'})
        self.assertIn('mode manual', json.loads(self._run('prompt', {'prompt': 'pace status'})[1])['reason'])
        auto = json.loads(self._run('prompt', {'prompt': 'pace auto'})[1])['reason']
        self.assertIn('mode auto', auto)
        self.assertIn('5h reduction on', auto)  # `auto` never touches the axis switches

    def test_axisToggle_showsInStatuslineWithoutWaitingForRecompute(self):
        self._run('statusline', self._status())
        self._run('prompt', {'prompt': 'pace 5h measure'})
        self.assertEqual(self._run('statusline', self._status())[1], 'cadence: [ok] r:off')

    def test_templateCommand_persistsAndFullStatuslineUsesIt(self):
        body = json.loads(self._run('prompt', {'prompt': '/pace template L2'})[1])
        self.assertIn('template L2', body['reason'])
        self.assertEqual(json.loads((Path(self.tmp.name) / 'ctl.json').read_text())['template'], 'L2')
        self.env['CLAUDE_STATUS_GIT'] = 'repo/develop (abc1234)'
        payload = dict(self._status(), model={'display_name': 'Claude Sonnet 5.5'}, effort={'level': 'high'},
                       context_window={'used_percentage': 42})
        code, out = self._run('statusline', payload, '--full')
        self.assertEqual(code, 0)
        rows = out.split('\n')
        self.assertEqual(rows[0], 'Sonnet 5.5 (High) | context 42%  | cadence [off] | repo/develop (abc1234)')
        self.assertTrue(rows[1].startswith('5h    ') and rows[2].startswith('week  '), out)

    def test_fullStatusline_noTemplateChosen_returnsTheCadenceSegmentInOneCall(self):
        code, out = self._run('statusline', self._status(), '--full')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(pacing.SEGMENT_MARK + 'cadence: [off]'), out)  # shell reuses it: one python start
        self.assertTrue(list(Path(self.tmp.name).glob('samples.s1.jsonl')))

    def test_templateC2_thenOff_returnsToTheShellLayout(self):
        self.env['CLAUDE_STATUS_GIT'] = 'repo/develop (abc1234)'
        self._run('prompt', {'prompt': 'pace template c2'})
        out = self._run('statusline', dict(self._status(), model={'display_name': 'Opus 5.5'}), '--full')[1]
        rows = out.split('\n')
        self.assertEqual(rows[0], 'Opus 5.5 | repo/develop (abc1234)')
        self.assertTrue(rows[1].startswith('> 5h 37% ('), out)
        self.assertTrue(rows[-1].startswith('> [off]'), out)
        self._run('prompt', {'prompt': 'pace template off'})
        self.assertTrue(self._run('statusline', self._status(), '--full')[1].startswith(pacing.SEGMENT_MARK + 'cadence:'))
        self._run('prompt', {'prompt': 'pace template c1'})
        tpl = self._run('statusline', dict(self._status(), model={'display_name': 'cadence: evil'}), '--full')[1]
        self.assertFalse(tpl.startswith(pacing.SEGMENT_MARK), tpl)  # a template line never carries the mark

    def test_fullStatusline_failsOpenWithNoOutput(self):
        self._run('prompt', {'prompt': 'pace template L1'})
        for bad in (['not', 'a', 'dict'], dict(self._status(), model='x'),
                    {'rate_limits': {'five_hour': {'used_percentage': '63'}, 'seven_day': {'used_percentage': 1}}}):
            self.assertEqual(self._run('statusline', bad, '--full'), (0, ''), bad)

    def test_enforce_levelZeroDecision_stillSleepsFiveHourDelay(self):
        self._run('prompt', {'prompt': 'pace 5h on'})  # decision still computed for the previous axis set
        dec = {'ts': time.time(), 'state': 'ok', 'level': 0, 'delay_rec': 0, 'delay_wk': 0, 'delay_5h': 1.2,
               'axes': ['wk']}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        t = time.monotonic()
        self.assertEqual(self._run('enforce', {'tool_name': 'Bash', 'session_id': 's1'}), (0, ''))
        self.assertGreaterEqual(time.monotonic() - t, 1.2)

    def test_enforce_fiveHourAxisOnly_sleepsDelay5(self):
        self._run('prompt', {'prompt': 'pace 5h on'})
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'delay_rec': 0, 'delay_wk': 0, 'delay_5h': 1.2}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        t = time.monotonic()
        self.assertEqual(self._run('enforce', {'tool_name': 'Bash', 'session_id': 's1'}), (0, ''))
        self.assertGreaterEqual(time.monotonic() - t, 1.2)

    def test_prompt_fiveHourOnly_noticeDoesNotBlameWeekly(self):
        self._run('prompt', {'prompt': 'pace 5h on'})
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'lead': 33.0, 'delay_rec': 5,
               'deficit5': 2.0, 'delay_5h': 5}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        out = self._run('prompt', {'prompt': 'fix the bug'})[1]
        self.assertIn('pacing L1', out)
        self.assertIn('5h window', out)
        self.assertNotIn('weekly', out)

    def test_prompt_bothAxes_causedByFiveHour_doesNotBlameWeekly(self):
        self._run('prompt', {'prompt': 'pace on'})
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'lead': -36.0, 'delay_rec': 5,
               'deficit5': 2.0, 'delay_5h': 5}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        out = self._run('prompt', {'prompt': 'fix the bug'})[1]
        self.assertIn('5h window', out)
        self.assertNotIn('weekly', out)

    def test_prompt_levelThree_alwaysExplainsTheFullWindow(self):
        self._run('prompt', {'prompt': 'pace wk on'})
        dec = {'ts': time.time(), 'state': 'L3', 'level': 3, 'lead': -5.0, 'delay_rec': 45}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        out = self._run('prompt', {'prompt': 'fix the bug'})[1]
        self.assertIn('pacing L3', out)
        self.assertIn('5h window is nearly full', out)

    def test_prompt_measuringAxisOnly_staysSilent(self):
        (Path(self.tmp.name) / 'ctl.json').write_text(json.dumps(
            {'wk': {'on': True}, 'h5': {'measure': True}, 'step': 2}))
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'lead': -36.0, 'delay_rec': 0,
               'deficit5': 2.0, 'delay_5h': 5}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        self.assertEqual(self._run('prompt', {'prompt': 'fix the bug'}), (0, ''))

    def test_promptOrdinary_isSilent(self):
        self.assertEqual(self._run('prompt', {'prompt': 'fix the bug'}), (0, ''))

    def test_enforce_on_sleepsDecidedDelay(self):
        self._run('prompt', {'prompt': 'pace on'})
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'delay_rec': 1.2}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        t = time.monotonic()
        code, out = self._run('enforce', {'tool_name': 'Bash', 'session_id': 's1'})
        self.assertEqual((code, out), (0, ''))
        self.assertGreaterEqual(time.monotonic() - t, 1.2)

    def test_enforce_level2_deniesThirdAgentButNotReviewer(self):
        self._run('prompt', {'prompt': 'pace on'})
        dec = {'ts': time.time(), 'state': 'L2', 'level': 2, 'delay_rec': 0}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        spawn = lambda t: self._run('enforce', {'tool_name': 'Agent', 'session_id': 's1', 'tool_input': {'subagent_type': t}})
        self.assertEqual(spawn('general-purpose')[1], '')
        self.assertEqual(spawn('general-purpose')[1], '')
        third = json.loads(spawn('general-purpose')[1])
        self.assertEqual(third['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertNotIn('weekly', third['hookSpecificOutput']['permissionDecisionReason'])
        self.assertEqual(spawn(EXEMPT[0])[1], '')

    def test_enforce_level2_exemptAgentsAreNotCounted(self):
        self._run('prompt', {'prompt': 'pace on'})
        dec = {'ts': time.time(), 'state': 'L2', 'level': 2, 'delay_rec': 0}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        for _ in range(4):
            out = self._run('enforce', {'tool_name': 'Agent', 'session_id': 's1', 'tool_input': {'subagent_type': EXEMPT[1]}})[1]
            self.assertEqual(out, '')
        self.assertEqual(list(Path(self.tmp.name, 'agents').glob('*')), [])

    def test_prompt_noticeDoesNotReenableAfterOff(self):
        self._run('prompt', {'prompt': 'pace on'})
        dec = {'ts': time.time(), 'state': 'L1', 'level': 1, 'lead': 33.0, 'delay_rec': 5}
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(dec))
        code, out = self._run('prompt', {'prompt': 'fix the bug'})
        self.assertIn('pacing L1', out)
        self._run('prompt', {'prompt': 'pace off'})
        self.assertFalse(json.loads((Path(self.tmp.name) / 'ctl.json').read_text())['wk']['on'])

    def test_enforce_garbageStdin_failsOpen(self):
        r = subprocess.run([sys.executable, '-S', str(HOOKS / 'pacing.py'), 'enforce'],
                           input='not json', capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ''))

    def _decision_file(self):
        (Path(self.tmp.name) / 'decision.json').write_text(json.dumps(
            {'ts': time.time(), 'state': 'L1', 'level': 1, 'lead': -25.0, 'u5': 24, 'u7': 45,
             'r5': int(time.time()) + 6600, 'now_rate5': 17.6, 'need_rate5': 21.09, 'deficit5': 0.56,
             'reduce5_pct': 0, 'delay_5h': 5.0, 'delay_rec': 5.0, 'delay_wk': 0.0}))

    def test_ctlBrief_printsTheBriefAndLeavesTheStateAlone(self):
        self._run('ctl', None, '5h', 'on')
        self._decision_file()
        before = (Path(self.tmp.name) / 'ctl.json').read_text()
        code, out = self._run('ctl', None, 'brief')
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('pace [L1]'), out)
        self.assertEqual((Path(self.tmp.name) / 'ctl.json').read_text(), before)

    def test_promptBrief_blocksWithTheBriefAndWritesNothing(self):
        self._run('ctl', None, '5h', 'on')
        self._decision_file()
        before = (Path(self.tmp.name) / 'ctl.json').read_text()
        code, out = self._run('prompt', {'prompt': '/pace brief'})
        body = json.loads(out)
        self.assertEqual((code, body['decision']), (0, 'block'))
        self.assertTrue(body['reason'].startswith('pace [L1]'), body['reason'])
        self.assertEqual((Path(self.tmp.name) / 'ctl.json').read_text(), before)


if __name__ == '__main__':
    unittest.main()
