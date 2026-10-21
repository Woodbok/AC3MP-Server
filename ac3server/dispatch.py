"""Which handler answers which call: (protocol id, method id) -> handler(call_id, params, addr) -> (label, reply).

AUTH_DISPATCH serves the authentication port, SECURE_DISPATCH the secure one. A call with no entry is logged as
"No handler" and left unanswered - in the game that is a 15-20 second stall, so every call the game makes has an
entry, even where the answer is an empty list. The protocol names and numbers are AC3's own, from its protocol
registration table.
"""

from ac3server.gamesession.groups import handle_gamesession_send_text_message
from ac3server.gamesession.invitations import (handle_gamesession_accept_invitation,
    handle_gamesession_decline_invitation, handle_gamesession_get_invitations_received,
    handle_gamesession_send_invitation)
from ac3server.gamesession.search import (handle_gamesession_search_sessions,
    handle_gamesession_search_with_participants)
from ac3server.gamesession.sessions import (handle_gamesession_abandon_session, handle_gamesession_add_participants,
    handle_gamesession_create_session, handle_gamesession_get_participants, handle_gamesession_get_session,
    handle_gamesession_join_session, handle_gamesession_leave_session, handle_gamesession_migrate_session,
    handle_gamesession_register_urls, handle_gamesession_remove_participants, handle_gamesession_update_session)
from ac3server.services.authentication import handle_login, handle_loginex, handle_requestticket
from ac3server.services.clientlogin import (CLIENTLOGIN_PROTO, handle_clientlogin_authenticate,
    handle_clientlogin_method3, handle_clientlogin_method5, handle_clientlogin_method7, handle_clientlogin_servertime)
from ac3server.services.contacts import (handle_contacts_game_sessions, handle_contacts_method5,
    handle_contacts_method6, handle_contacts_resolve_pids)
from ac3server.services.friends import (FRIENDS_PROTO, handle_friends_add_by_name_with_details,
    handle_friends_add_with_details, handle_friends_get_detailed_list)
from ac3server.services.gameinfo import (handle_gameinfo_getfilelist, handle_gameinfo_method6,
    handle_news_get_channels_by_types, handle_persistentstore_getitem)
from ac3server.services.ladder import (handle_ladder_end_game, handle_ladder_get_reward_bitfield,
    handle_ladder_participate, handle_ladder_register_new_game)
from ac3server.services.misc import (MESSAGING_PROTO, handle_eligibility_check, handle_liveevents_method1,
    handle_locale_announce, handle_messaging_get_headers, handle_self_profile_lookup, handle_trackingext_ping)
from ac3server.services.nattraversal import handle_nat_request_probe_initiation
from ac3server.services.onlinesave import handle_onlinesave_get, handle_onlinesave_set
from ac3server.services.overlord import (OVERLORD_CORE_PROTO, OVERLORD_DARES_PROTO, OVERLORD_FRIENDS_PROTO,
    OVERLORD_NEWS_PROTO, handle_overlord_core_settings, handle_overlord_dares, handle_overlord_friends,
    handle_overlord_news)
from ac3server.services.presence import handle_richpresence_get, handle_richpresence_set
from ac3server.services.secure import handle_register, handle_registerex
from ac3server.services.shop import (FRIENDSLEAGUE_PROTO, SHOP_PROTO, WALLET_PROTO, handle_friendsleague_goals,
    handle_friendsleague_save_progress, handle_shop_add_currency, handle_shop_bought_items, handle_shop_buy_item,
    handle_shop_catalog, handle_shop_get_currency, handle_shop_unlock_item, handle_wallet_get_url)
from ac3server.services.statistics import (handle_playerstatistics_leaderboard_by_names,
    handle_playerstatistics_leaderboard_for_player, handle_playerstatistics_leaderboard_range,
    handle_playerstatistics_read, handle_playerstatistics_send)
from ac3server.services.uplaywin import (handle_uplaywin_get_rewards, handle_uplaywin_get_rewards_purchased,
    handle_uplaywin_welcome)


AUTH_DISPATCH = {
    # Authentication 0x0A
    (0x0A, 1): handle_login,                                # Login (the "Tracking" telemetry account)
    (0x0A, 2): handle_loginex,                              # LoginEx (the player)
    (0x0A, 3): handle_requestticket,
}


SECURE_DISPATCH = {
    # SecureConnection 0x0B
    (0x0B, 1): handle_register,
    (0x0B, 4): handle_registerex,

    # GameSession 0x2A - matchmaking (gamesession/)
    (0x2A, 1): handle_gamesession_create_session,           # CreateSession
    (0x2A, 2): handle_gamesession_update_session,           # UpdateSession
    (0x2A, 4): handle_gamesession_migrate_session,          # MigrateSession
    (0x2A, 5): handle_gamesession_leave_session,            # LeaveSession
    (0x2A, 6): handle_gamesession_get_session,              # GetSession
    (0x2A, 7): handle_gamesession_search_sessions,          # SearchSessions
    (0x2A, 8): handle_gamesession_add_participants,         # AddParticipants
    (0x2A, 9): handle_gamesession_remove_participants,      # RemoveParticipants
    (0x2A, 11): handle_gamesession_get_participants,        # GetParticipants
    (0x2A, 12): handle_gamesession_send_invitation,         # SendInvitation
    (0x2A, 14): handle_gamesession_get_invitations_received,  # GetInvitationsReceived
    (0x2A, 17): handle_gamesession_accept_invitation,       # AcceptInvitation
    (0x2A, 18): handle_gamesession_decline_invitation,      # DeclineInvitation
    (0x2A, 20): handle_gamesession_send_text_message,       # SendTextMessage (a group's broadcasts)
    (0x2A, 21): handle_gamesession_register_urls,           # RegisterURLs
    (0x2A, 22): handle_gamesession_join_session,            # JoinSession
    (0x2A, 23): handle_gamesession_abandon_session,         # AbandonSession
    (0x2A, 24): handle_gamesession_search_with_participants,  # SearchSessionsWithParticipants
    # NATTraversal 0x03
    (0x03, 1): handle_nat_request_probe_initiation,         # RequestProbeInitiation

    # Friends 0x14, ContactsExtensions 0x72, RichPresence 0x6D, Messaging 0x17
    (FRIENDS_PROTO, 3): handle_friends_add_with_details,    # AddFriendWithDetails
    (FRIENDS_PROTO, 4): handle_friends_add_by_name_with_details,  # AddFriendByNameWithDetails
    (FRIENDS_PROTO, 12): handle_friends_get_detailed_list,  # GetDetailedList
    (0x72, 3): handle_contacts_game_sessions,               # RetrieveGameSessionFromContact
    (0x72, 4): handle_contacts_resolve_pids,                # pids -> names
    (0x72, 5): handle_contacts_method5,
    (0x72, 6): handle_contacts_method6,
    (0x6D, 1): handle_richpresence_set,                     # SetPresence
    (0x6D, 2): handle_richpresence_get,                     # GetPresence
    (MESSAGING_PROTO, 4): handle_messaging_get_headers,     # GetMessagesHeaders

    # The player's progress: OnlineSave 0x77, PlayerStatistics 0x6C, Ladder 0x7A
    (0x77, 1): handle_onlinesave_set,                       # the profile blob, uploaded
    (0x77, 2): handle_onlinesave_get,                       # the profile blob, at login
    (0x6C, 2): handle_playerstatistics_send,                # SendPlayerStats (a match's numbers)
    (0x6C, 3): handle_playerstatistics_read,                # ReadPlayerStats (the career)
    (0x6C, 4): handle_playerstatistics_leaderboard_range,   # ReadStatsLeaderboardByRange (a page of a board)
    (0x6C, 5): handle_playerstatistics_leaderboard_for_player,  # ...ByRangeForPlayer (around a player)
    (0x6C, 6): handle_playerstatistics_leaderboard_by_names,  # ...ByPlayerNames (friends)
    (0x7A, 1): handle_ladder_register_new_game,             # registerNewGame (the host: a game id)
    (0x7A, 2): handle_ladder_participate,                   # participateToGame
    (0x7A, 3): handle_ladder_end_game,                      # endGame
    (0x7A, 5): handle_ladder_get_reward_bitfield,           # getRewardBitfield

    # Shop 0x1B5B, Wallet 0x1A0A, FriendsLeague 0x1C20, UplayWin 0x31
    (SHOP_PROTO, 1): handle_shop_catalog,                   # the catalogue
    (SHOP_PROTO, 3): handle_shop_add_currency,              # credits earned
    (SHOP_PROTO, 4): handle_shop_get_currency,              # the balance
    (SHOP_PROTO, 5): handle_shop_buy_item,
    (SHOP_PROTO, 6): handle_shop_unlock_item,
    (SHOP_PROTO, 7): handle_shop_bought_items,              # RequestBoughtItems
    (WALLET_PROTO, 1): handle_wallet_get_url,               # the Extra Content button's URL
    (FRIENDSLEAGUE_PROTO, 1): handle_friendsleague_goals,
    (FRIENDSLEAGUE_PROTO, 2): handle_friendsleague_save_progress,
    (0x31, 5): handle_uplaywin_get_rewards,
    (0x31, 6): handle_uplaywin_get_rewards_purchased,
    (0x31, 7): handle_uplaywin_welcome,                     # UplayWelcome

    # The online init sequence: settings, content, and services answered with empty or fixed replies
    (0x71, 6): handle_gameinfo_method6,
    (0x71, 7): handle_gameinfo_getfilelist,                 # GameInfo GetFileList (the game settings CXB)
    (0x18, 4): handle_persistentstore_getitem,
    (0x1F, 2): handle_news_get_channels_by_types,
    (0x6F, 1): handle_trackingext_ping,
    (0x23, 1): handle_eligibility_check,                    # the error-code table
    (0x1D, 3): handle_self_profile_lookup,
    (0x27, 2): handle_locale_announce,
    (0x78, 1): handle_liveevents_method1,
    (CLIENTLOGIN_PROTO, 1): handle_clientlogin_authenticate,  # ClientLogin 0x1BBC
    (CLIENTLOGIN_PROTO, 3): handle_clientlogin_method3,
    (CLIENTLOGIN_PROTO, 4): handle_clientlogin_servertime,
    (CLIENTLOGIN_PROTO, 5): handle_clientlogin_method5,
    (CLIENTLOGIN_PROTO, 7): handle_clientlogin_method7,
    (OVERLORD_NEWS_PROTO, 1): handle_overlord_news,         # Overlord middleware 0x138A-0x1390
    (OVERLORD_CORE_PROTO, 1): handle_overlord_core_settings,
    (OVERLORD_FRIENDS_PROTO, 1): handle_overlord_friends,
    (OVERLORD_DARES_PROTO, 1): handle_overlord_dares,
}
