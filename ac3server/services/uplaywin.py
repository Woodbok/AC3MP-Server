"""UplayWin (protocol 0x31): Uplay's rewards service, as the game calls it through Rendez-Vous. Every list is empty.

    m5 GetRewards           [i32 start][i32 max][string sort][string culture][string platform][string game]
                            -> [list] rewards
    m6 GetRewardsPurchased  (the same)
    m7 UplayWelcome         [string culture][string platform][string game]   -> [list] actions

From the DDL embedded in the exe (UplayWin). Ubisoft answered UplayWelcome with an empty list. For GetRewards it sent
three Uplay rewards for a points service that no longer exists; an empty list is the honest answer and the shape
the game accepts for m7.
"""

import struct

from ac3server.protocol.rmc import build_rmc_response


def handle_uplaywin_welcome(call_id, params, addr):
    return "UplayWin.UplayWelcome", build_rmc_response(0x31, call_id, 7, struct.pack('<I', 0))


def handle_uplaywin_get_rewards(call_id, params, addr):
    return "UplayWin.GetRewards", build_rmc_response(0x31, call_id, 5, struct.pack('<I', 0))


def handle_uplaywin_get_rewards_purchased(call_id, params, addr):
    return "UplayWin.GetRewardsPurchased", build_rmc_response(0x31, call_id, 6, struct.pack('<I', 0))
