"""Guided practice flow for the airdrop driving game."""

from __future__ import annotations

from psynet.modular_page import Control, ModularPage
from psynet.participant import Participant

if __package__:
    from . import game
else:
    import game


def tutorial_steps(condition: str | None = None) -> list[str]:
    """Return the tutorial steps enabled for the selected condition."""

    if condition is None:
        condition = game.EXPERIMENT_CONDITION

    steps = [
        "movement",
        "overview_map",
        "airdrop",
        "airplane_sighting",
        "landing_zone_medium",
        "landing_zone_high",
    ]
    if condition in {"social", "live"}:
        steps.append("other_car")
    return steps


def tutorial_player(participant_id: int) -> dict:
    """Create the deterministic player used in the solo driving tutorial."""

    return {
        "participant_id": str(participant_id),
        "label": "Your car",
        "color": game.PLAYER_COLORS[0],
        "x": 300.0,
        "y": 320.0,
        "vx": 0.0,
        "vy": 0.0,
        "heading": 0.0,
    }


def tutorial_answer(participant_id: int) -> dict:
    """Return the structured tutorial answer used by PsyNet bots."""

    steps = tutorial_steps()
    return {
        "completed_tutorial": True,
        "experiment_condition": game.EXPERIMENT_CONDITION,
        "participant_id": int(participant_id),
        "completed_steps": steps,
        "step_elapsed_ms": {step: 0 for step in steps},
    }

class TutorialControl(game.GameAssets, Control):
    """Deterministic solo tutorial using the production driving renderer."""

    external_template = "game.html"
    macro = "game_control"

    def __init__(self, participant):
        self.participant = participant
        super().__init__(show_next_button=False)

    @property
    def canvas_config(self):
        participant_id = int(self.participant.id)
        player = tutorial_player(participant_id)
        reward = {
            "id": "tutorial-reward",
            "start_ms": 0,
            "end_ms": 10 * 60 * 1000,
            "x": 308,
            "y": 320,
            "radius": 2.5,
            "line": [],
        }
        return {
            "mode": "tutorial",
            "experiment_condition": game.EXPERIMENT_CONDITION,
            "role": player["label"],
            "participant_id": participant_id,
            "canvas_size": game.CANVAS_SIZE,
            "canvas_width": game.CANVAS_RENDER_WIDTH,
            "canvas_height": game.CANVAS_RENDER_HEIGHT,
            "trial_seconds": 10 * 60,
            "send_interval_ms": game.SEND_INTERVAL_MS,
            "draw_interval_ms": game.DRAW_INTERVAL_MS,
            "player_radius": game.PLAYER_RADIUS,
            "initial_players": {str(participant_id): player},
            "initial_player": player,
            "coin_radius": reward["radius"],
            "coin_bonus": game.COIN_BONUS,
            "max_player_speed": game.TUTORIAL_MAX_PLAYER_SPEED,
            "speed_limit_mph": 60,
            "projection": {"px_per_mile": 1},
            "storm_points": [[0, 10 * 60 * 1000, 320, 320, 75, 80, 0]],
            "warnings": [
                {
                    "event": "Severe Thunderstorm Warning",
                    "eff_ms": 0,
                    "exp_ms": 10 * 60 * 1000,
                    "polygons": [
                        [[330, 314], [342, 314], [342, 326], [330, 326]]
                    ],
                },
                {
                    "event": "Tornado Warning",
                    "eff_ms": 0,
                    "exp_ms": 10 * 60 * 1000,
                    "polygons": [
                        [[346, 314], [358, 314], [358, 326], [346, 326]]
                    ],
                },
            ],
            "reward_events": [reward],
            "chaser_tracks": [],
            "timing": {
                "game_start_ms": 0,
                "game_end_ms": 10 * 60 * 1000,
            },
            "tutorial": {
                "steps": tutorial_steps(),
                "intro_duration_ms": 5_000,
                "cue_blink_period_ms": 800,
                "overview_play_duration_ms": 4_000,
                "final_car_duration_ms": 15_000,
                "movement_threshold": game.TUTORIAL_MOVEMENT_THRESHOLD,
                "movement_min_duration_ms": 5_000,
                "reward_id": reward["id"],
                "reward_target_distance": 8.0,
                "sighting_reach_radius": 3.0,
                "sighting_target_distance": 12.0,
                "zone_target_distance": 14.0,
                "airplane_sighting_hint": {"x": 320, "y": 320},
                "medium_zone_hint": {"x": 336, "y": 320},
                "high_zone_hint": {"x": 352, "y": 320},
                "demo_car": {
                    "participant_id": "tutorial:other-car",
                    "label": "Another driver",
                    "color": game.PLAYER_COLORS[1],
                    "orbit_radius": 3.5,
                    "orbit_seconds": 8,
                },
            },
        }

    def format_answer(self, raw_answer, **kwargs):
        return raw_answer

    def get_bot_response(self, experiment, bot, page, prompt):
        return tutorial_answer(bot.id)


def tutorial_page(participant: Participant):
    """Show the condition-aware guided driving tutorial."""

    return ModularPage(
        game.TUTORIAL_PAGE_LABEL,
        "",
        TutorialControl(participant),
        save_answer="guided_driving_tutorial",
        time_estimate=150,
        requires_full_page_reload=True,
    )
