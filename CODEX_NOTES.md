# Strata Motion Fix — Reference for Codex

## What was wrong

Draw moves were buzzy/staccato. Travel moves were fine. Walk Home was unreliable.

## Root cause: SVG oversampling + serial bottleneck

Strata's SVG processor samples all paths at 0.5mm intervals (`SAMPLE_INTERVAL_MM = 0.5` in `svg_processor.py`). A 100mm straight line becomes 200+ points. The motion planner creates a segment for every adjacent point pair. Between near-collinear segments, the corner velocity formula returns near-zero — so the motor must accelerate and decelerate for every 0.5mm segment.

At 9600 baud, each LM command takes ~50-60ms to transmit over serial. A 0.5mm segment at 80 mm/s executes in 6ms. The motor finishes each tiny move and sits idle waiting for the next command. This creates audible gaps = buzz.

Saxi never has this problem because `flatten-svg` uses adaptive sampling (only adds points where curves actually curve), so paths have far fewer points.

## What was fixed (commit 603f76d)

### 1. Douglas-Peucker path simplification
Added `_simplify_path()` in `serial_manager.py`. Before motion planning, all draw paths are simplified with 0.02mm tolerance. This collapses collinear points while preserving curve shape. A 200-point straight line becomes 2 points. Curves keep enough points for accuracy but far fewer than the 0.5mm sampling produced.

### 2. Saxi-matched acceleration defaults
Old defaults were wrong. Fixed to match Saxi's `planning.ts` and `massager.ts`:
- Draw acceleration: 200 mm/s² (was 300)
- Travel acceleration: 400 mm/s² (was 300)
- Draw speed default: 50 mm/s (was 25)
- Travel speed default: 200 mm/s (was 75)
- Travel corner factor: 0.0 (was 0.127 — travel doesn't need cornering)

### 3. Replaced broken `_try_lm_move`
The old function used a hand-rolled formula (`initial_rate=0`, `final_rate = 2d/t * 80`) that didn't match Saxi at all. Replaced `_move_with_planner` to use the real `_plan_path_blocks` planner for all moves (both draw and travel). This gives proper triangle/trapezoid velocity profiles matching Saxi's `constantAccelerationPlan`.

### 4. FIFO pacing
Added `_wait_for_fifo_space()` — polls QM every 8 LM blocks to prevent FIFO overflow on the EBB.

### 5. Walk Home fix
Old code used `_move_to(0, 0)` which went through the command queue and could accumulate position drift. New code reads position directly, resets step error accumulators, and syncs `_commanded_x/y` on completion.

## Things NOT to do

- **Do NOT use per-segment XM moves for drawing.** Commit `195fd0b` tried this and it buzzed terribly. Always use the multi-segment planner + LM pipeline.
- **Do NOT use `HM,4000` for walk home.** It was tried and broke the machine. Saxi only uses HM in `postCancel` (abort recovery). Normal homing is a planned pen-up move back to (0,0).
- **Do NOT change `SAMPLE_INTERVAL_MM` in svg_processor.py** to try to fix motion. The 0.5mm sampling is fine for preview display. Path simplification in serial_manager handles the motion side.
- **Do NOT remove the Douglas-Peucker simplification.** It is the primary fix for buzzy motion. Without it, the planner creates too many tiny segments that the serial link can't feed fast enough.

## Saxi reference: how the pipeline works

1. Paths start in mm coordinates
2. `massager.ts replan()` converts to logical steps: `paths.map(ps => ps.map(p => vmul(p, device.stepsPerMm)))` (×5)
3. Acceleration/velocity are also in logical steps: `accel * 5`, `vel * 5`
4. `constantAccelerationPlan()` plans in logical-step space with triangle/trapezoid profiles
5. `executeBlockWithLM()` converts to microsteps: `(p2 - p1) * stepMultiplier` (×16) with error accumulation
6. `moveWithAcceleration()` converts XY to CoreXY: `axis1 = x+y`, `axis2 = x-y`
7. `axisRate()` computes LM parameters: `initialRate = stepsPerSec * (0x80000000 / 25000)`

Strata's planner works in mm directly (not logical steps), which is mathematically equivalent as long as the ×80 conversion happens correctly at execution time in `_execute_lm_block`. This is fine and verified working.

## Key constants
```
stepsPerMm = 5 (logical steps per mm)
microstepFactor = 16 (EM,1,1 mode)
microstepsPerMm = 80
LM rate scale = 0x80000000 / 25000 = 85899.3459...
```
