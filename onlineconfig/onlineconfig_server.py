"""The HTTP side of the server (TCP 80): the game's first request, and /status. Served by waitress
(requirements.txt).

    GET  /OnlineConfigService.svc/GetOnlineConfig   the OnlineConfig document: where the game servers are
    GET  /status                                    200 when this process and the game server answer, else 503
    GET  /                                          one line saying what this is

The game reaches this because its OnlineConfig host name is pointed here: a client patched with
patcher/ac3mp_patch.py --host, or a hosts-file line. The document is the one onlineconfigservice.ubi.com served for
AC3 PC, with our address in place of the game servers' and of the NAT detect servers' when `nat_detect` is "own".
"""
import json
import os
import sys
import time
import urllib.request

from flask import Flask, request, Response

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from server_config import NAT_DETECT, NAT_DETECT_ADDRESS2, NAT_DETECT_PORT, STATUS_PORT  # noqa: E402
from server_config import SERVER_ADDRESS, address_problem, describe  # noqa: E402

app = Flask(__name__)
STARTED = time.time()
VERSION = os.environ.get("AC3_SERVER_VERSION", "dev")
HTTP_PORT = int(os.environ.get("AC3_HTTP_PORT", "80"))

# The address handed to the game for our own services (server_config.json `address`, AC3_SERVER_ADDRESS, or found at
# start-up): the address players reach this server at - its public one on the internet, its LAN one at a LAN party.
LOCAL_IP = SERVER_ADDRESS


def detect_urls():
    """The two NAT detect servers the game asks at every start (server_config nat_detect; ac3server/natdetect.py)."""
    if NAT_DETECT != "own":
        return ["lb-ne1z-prod-mpe-detect01.ubisoft.com:11000", "lb-ne1z-prod-mpe-detect02.ubisoft.com:11000"]
    second = f"{NAT_DETECT_ADDRESS2}:{NAT_DETECT_PORT}" if NAT_DETECT_ADDRESS2 else f"{LOCAL_IP}:{NAT_DETECT_PORT + 2}"
    return [f"{LOCAL_IP}:{NAT_DETECT_PORT}", second]


def make_response():
    """The OnlineConfig document. SandboxUrl is the authentication server; SandboxUrlPriv and SandboxUrlWS are
    advertised as Ubisoft did, but the game does not contact either. The US_* Uplay web services are Ubisoft's and
    gone: the Overlord settings tell the game to skip them (ac3server/services/overlord.py)."""
    return json.dumps([
        {"Name": "GenomeId", "Values": ["258b0be0-32e0-4eca-a034-c4aaef4648f8"]},
        {"Name": "killSwitch_contentFiltering", "Values": ["0"]},
        {"Name": "killSwitch_friendsProposal", "Values": ["0"]},
        {"Name": "killSwitch_friendsRequest", "Values": ["0"]},
        {"Name": "killSwitch_friendsSuggestions", "Values": ["0"]},
        {"Name": "killSwitch_socialFeed", "Values": ["0"]},
        {"Name": "killSwitch_socialFeedWrites", "Values": ["0"]},
        {"Name": "killSwitch_uplay", "Values": ["0"]},
        {"Name": "punch_DetectUrls", "Values": detect_urls()},
        {"Name": "SandboxUrl", "Values": [f"prudp:/address={LOCAL_IP};port=21006"]},
        {"Name": "SandboxUrlPriv", "Values": [f"prudp:/address={LOCAL_IP};port=21011"]},
        {"Name": "SandboxUrlWS", "Values": [f"{LOCAL_IP}:21005"]},
        {"Name": "UplayGameCode", "Values": ["AC3"]},
        {"Name": "US_AcceptSuggestion_Win32", "Values": ["https://friendsservice.ubi.com/friendsservice.svc/REST/AcceptSuggestion "]},
        {"Name": "US_CommentPost_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/InsertWallPostComment/"]},
        {"Name": "US_DeclineSuggestion_Win32", "Values": ["https://friendsservice.ubi.com/friendsservice.svc/REST/DeclineSuggestion "]},
        {"Name": "US_GetWallPostsByIds_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/GetWallPostsByIds/"]},
        {"Name": "US_GetWalls_Win32", "Values": ["https://uplay.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/GetWalls/ "]},
        {"Name": "US_GetWallsPost_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/GetWallPosts/"]},
        {"Name": "US_InitializeUser_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/WinServices/GameClientServices.svc/REST/JSON/InitializeUser/"]},
        {"Name": "US_InsertPost_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/InsertWallPost/"]},
        {"Name": "US_LikePost_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/InsertWallPostLike/"]},
        {"Name": "US_Login_Win32", "Values": ["https://signupservice.ubi.com/PublicSignupService.svc/REST/LogInAndGet "]},
        {"Name": "US_Logout_Win32", "Values": ["https://signupservice.ubi.com/PublicSignupService.svc/REST/LogOut "]},
        {"Name": "US_LookupExternalId_Win32", "Values": ["https://signupservice.ubi.com/PublicSignupService.svc/REST/LookupExternalAccountIDsByUbiAccountIDs "]},
        {"Name": "US_LookupUplayId_Win32", "Values": ["https://signupservice.ubi.com/PublicSignupService.svc/REST/LookupUbiAccountIDsByExternalAccountIDs "]},
        {"Name": "US_LookupUplayUsername_Win32", "Values": ["https://signupservice.ubi.com/PublicSignupService.svc/REST/LookupUsernamesByUbiAccountIDs "]},
        {"Name": "US_RequestFriends_Win32", "Values": ["https://friendsservice.ubi.com/friendsservice.svc/REST/GetRelationshipsWithPresenceByToken "]},
        {"Name": "US_RequestSuggestions_Win32", "Values": ["https://friendsservice.ubi.com/friendsservice.svc/REST/GetSuggestionsByPage "]},
        {"Name": "US_SharePost_Win32", "Values": ["https://wspuplay-ext.ubi.com/UplayServices/ShareServices/GameClientServices.svc/REST/JSON/ShareWallPost/"]},
        {"Name": "US_SyncFriends_Win32", "Values": ["https://friendsservice.ubi.com/friendsservice.svc/REST/ProposeContacts "]},
    ])


def handle_get_online_config():
    config_id = request.args.get('onlineConfigID')
    target = request.args.get('target')
    print(f'GetOnlineConfig request: onlineConfigID={config_id!r} target={target!r}')

    # Checked on every request: an address that stops being reachable after start-up (an "auto" address taken from a
    # VPN that has since gone) sends every game into limited mode with nothing in the game server's log.
    problem = address_problem()
    if problem:
        print('  *** WARNING: about to hand out an unreachable address ***')
        print(f'  {problem}')
        print('  The client will fall back to limited mode and the PRUDP log '
              'will stay empty.')
    print(f'  handing out SandboxUrl address {LOCAL_IP}')

    response = Response(make_response())
    response.headers['Content-Type'] = 'application/json'
    return response, 200


@app.route('/OnlineConfigService.svc/GetOnlineConfig', methods=['GET'])
def get_online_config():
    return handle_get_online_config()


@app.route('/OnlineConfigService.svc/GetOnlineConfigPSN', methods=['GET'])
def get_online_config_psn():
    return handle_get_online_config()


@app.route('/', methods=['GET'])
def index():
    return Response("ac3-server: an Assassin's Creed III multiplayer server. Point your game here with "
                    "patcher/ac3mp_patch.py --host " + LOCAL_IP + "\n", mimetype="text/plain")


@app.route('/status', methods=['GET'])
def status():
    """For a glance and for an uptime monitor: this process, and whether the PRUDP server answers.

    200 when both run; 503 when the PRUDP server is down or hung (its answer comes from its own
    secure thread, so a stuck server reads as not answering)."""
    body = {"ok": True, "service": "ac3-server", "version": VERSION, "http_uptime_s": int(time.time() - STARTED)}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{STATUS_PORT}/status", timeout=5) as r:
            prudp = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        prudp = {"ok": False, "message": f"not answering ({exc.__class__.__name__})"}
    body["prudp"] = prudp
    body["ok"] = bool(prudp.get("ok"))
    return Response(json.dumps(body), status=200 if body["ok"] else 503, mimetype="application/json")


if __name__ == '__main__':
    print(f"ac3-server {VERSION}")
    print(describe())
    # Never Flask's debug mode: the Werkzeug debugger's console runs any Python it is sent.
    try:
        from waitress import serve
    except ImportError:
        print("[http] waitress is not installed (pip install -r requirements.txt): Flask's development server")
        app.run(host='0.0.0.0', port=HTTP_PORT)
    else:
        print(f"[http] serving on 0.0.0.0:{HTTP_PORT} (waitress)")
        serve(app, host='0.0.0.0', port=HTTP_PORT, threads=8)
