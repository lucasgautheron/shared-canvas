"""Experiment entry point and condition selection for the airdrop game."""

from __future__ import annotations

from typing import List

from dallinger.experiment import experiment_route
from dallinger.experiment_server.utils import success_response
from dominate import tags
from flask import request

import psynet.experiment
from psynet.bot import BotDriver, advance_past_wait_pages
from psynet.db import with_transaction
from psynet.page import InfoPage
from psynet.participant import Participant
from psynet.sync import Barrier, SimpleGrouper
from psynet.timeline import PageMaker, Timeline
from psynet.trial.static import StaticTrialMaker
from psynet.consent import NoConsent

if __package__:
    from . import game, tutorial
else:
    import game
    import tutorial


EXPERIMENT_CONDITION = "social"
game.configure_experiment(EXPERIMENT_CONDITION)


def _introduction_page() -> InfoPage:
    """Build condition-specific instructions with one sentence per paragraph."""

    condition_paragraph = {
        "asocial": "You will play by yourself.",
        "social": (
            "You will play in real time with other players."
        ),
        "live": "You will play in real time with other players.",
    }[EXPERIMENT_CONDITION]

    content = tags.div()
    with content:
        tags.h2("Airdrop collection game")
        tags.p("You will navigate a map and collect airdrops.")
        tags.p("Each airdrop you collect gives you $0.10.")
        tags.p("You will receive hints to help you locate incoming airdrops.")
        tags.p(condition_paragraph)
        tags.p("Click Next to take the tutorial.")
        tags.img(
            src="/static/game_screenshot.png",
            alt="The airdrop driving game with its bonus, timer, and map legend.",
            style=(
                "display: block; width: 100%; max-width: 512px; "
                "margin: 1rem auto 0; border: 1px solid #d6dde8; "
                "border-radius: 12px;"
            ),
        )
    return InfoPage(content, time_estimate=10)


class Exp(psynet.experiment.Experiment):
    label = "Airdrop navigation"
    variables_initial_values = {
        "experiment_condition": EXPERIMENT_CONDITION,
        "group_size": game.GROUP_SIZE,
        "canvas_size": game.CANVAS_SIZE,
        "trial_seconds": game.TRIAL_SECONDS,
        "session_trials_per_participant": game.SESSION_TRIALS_PER_PARTICIPANT,
        "send_interval_ms": game.SEND_INTERVAL_MS,
        "draw_interval_ms": game.DRAW_INTERVAL_MS,
        "coin_bonus": game.COIN_BONUS,
        "storm_chaser_track_count": game.STORM_CHASER_TRACK_COUNT,
        "chaser_track_visible_ms": game.CHASER_TRACK_VISIBLE_MS,
        "soft_max_experiment_payment": 1000.0,
        "hard_max_experiment_payment": 1100.0,
        "max_participant_payment": 25.0,
        "soft_max_experiment_payment_email_sent": False,
        "hard_max_experiment_payment_email_sent": False,
    }

    timeline = Timeline(
        NoConsent(),
        _introduction_page(),
        PageMaker(tutorial.tutorial_page, time_estimate=90),
        InfoPage(
            "The tutorial is finished. Click Next when you are ready to play.",
            time_estimate=5,
        ),
        SimpleGrouper(
            group_type=game.GROUP_TYPE,
            initial_group_size=game.GROUP_SIZE,
            batch_size=game.GROUP_SIZE,
            waiting_logic=PageMaker(game.lobby_page, time_estimate=5),
            waiting_logic_expected_repetitions=1,
            max_wait_time=180,
        ),
        StaticTrialMaker(
            id_="game_worlds",
            trial_class=game.GameTrial,
            nodes=game.get_world_nodes,
            expected_trials_per_participant=game.SESSION_TRIALS_PER_PARTICIPANT,
            max_trials_per_participant=game.SESSION_TRIALS_PER_PARTICIPANT,
            allow_repeated_nodes=False,
            sync_group_type=game.GROUP_TYPE,
            check_performance_at_end=False,
        ),
    )

    test_n_bots = 4
    test_mode = "serial"

    @experiment_route("/lobby/waiting", methods=["GET"])
    @classmethod
    @with_transaction
    def lobby_waiting(cls):
        unique_id = request.args.get("unique_id")
        if not unique_id:
            return success_response(waiting=True, waiter_count=0)
        try:
            participant = cls.get_participant_from_unique_id(
                unique_id, for_update=False
            )
        except Exception:
            return success_response(waiting=True, waiter_count=0)
        waiting = game.GROUPER_ID in participant.active_barriers
        waiters = Barrier.get_waiting_participants_from_barrier_id(game.GROUPER_ID)
        return success_response(waiting=waiting, waiter_count=len(waiters))

    def test_serial_run_bots(self, bots: List[BotDriver]):
        assert tutorial.tutorial_steps("asocial") == [
            "movement",
            "overview_map",
            "airdrop",
            "airplane_sighting",
            "landing_zone_medium",
            "landing_zone_high",
        ]
        assert tutorial.tutorial_steps("social")[-1] == "other_car"
        assert tutorial.tutorial_steps("live")[-1] == "other_car"

        for bot in bots:
            assert bot.current_page_label == game.TUTORIAL_PAGE_LABEL
            bot.take_page(response=tutorial.tutorial_answer(bot.id))

        for bot in bots:
            participant = Participant.query.get(bot.id)
            answer = participant.var.guided_driving_tutorial
            assert answer["completed_tutorial"] is True
            assert answer["experiment_condition"] == EXPERIMENT_CONDITION
            assert answer["completed_steps"] == tutorial.tutorial_steps()

        for bot in bots:
            assert (
                "The tutorial is finished. Click Next when you are ready to play."
                in bot.current_page_text
            )
            bot.take_page()

        game.advance_past_lobby_pages(bots)
        advance_past_wait_pages(bots)

        world_ids_by_participant = {bot.id: [] for bot in bots}
        for trial_index in range(game.SESSION_TRIALS_PER_PARTICIPANT):
            for bot in bots:
                assert bot.current_page_label == "game"
                bot.take_page(response=game.build_bot_answer(bot))

            advance_past_wait_pages(bots)

            answers_by_group = {}
            for bot in bots:
                assert "Navigation complete" in bot.current_page_text
                answer = bot.current_trial.answer
                assert isinstance(answer, dict)
                assert answer["completed_live_canvas"] is True
                assert answer["experiment_condition"] == EXPERIMENT_CONDITION
                assert answer["coin_bonus"] == 0.0
                assert answer["collected_coin_ids"] == []
                assert answer["collected_public_coin_ids"] == []
                assert answer["participant_id"] == bot.id
                world_ids_by_participant[bot.id].append(answer["world_id"])
                participant = Participant.query.get(bot.id)
                group_id = int(participant.active_sync_groups[game.GROUP_TYPE].id)
                answers_by_group.setdefault(group_id, []).append(answer)

            assert len(answers_by_group) == len(bots) // game.GROUP_SIZE
            assert all(
                len(group_answers) == game.GROUP_SIZE
                for group_answers in answers_by_group.values()
            )

            if trial_index < game.SESSION_TRIALS_PER_PARTICIPANT - 1:
                for bot in bots:
                    bot.take_page()
                advance_past_wait_pages(bots)

        assert all(
            len(world_ids) == game.SESSION_TRIALS_PER_PARTICIPANT
            and len(set(world_ids)) == game.SESSION_TRIALS_PER_PARTICIPANT
            for world_ids in world_ids_by_participant.values()
        )
