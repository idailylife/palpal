"""Run the production save validator without requiring game assets or SDL."""
from pathlib import Path
import subprocess

from tests.test_native_object_pixels import c_function


def test_save_validation_bounds_and_two_legal_followers(tmp_path):
    native = (Path(__file__).parents[1] / "vendor/sdlpal/global.c").read_text()
    validator = c_function(native, "static BOOL\nPAL_ValidateSaveData(")
    source = r"""
#include <assert.h>
#include <stddef.h>
#include <string.h>
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#define MAX_EVENT_OBJECTS 10
#define MAX_PLAYERS_IN_PARTY 3
#define MAX_PLAYABLE_PLAYER_ROLES 5
#define MAX_PLAYER_ROLES 6
#define MAX_SCENES 300
#define DO_BYTESWAP(s,size) ((void)0)
typedef struct { int unused; } EVENTOBJECT;
typedef struct {
    unsigned short nPartyMember, wNumScene, wPartyDirection, nFollower;
    struct { unsigned short wPlayerRole; } rgParty[5];
    struct { unsigned short wEventObjectIndex; } rgScene[300];
} SAVED, *LPSAVEDGAME_COMMON;
static struct { struct { void *fpSSS; } f; } globals, *gpGlobals = &globals;
static int PAL_MKFGetChunkSize(int chunk, void *file) { return 3 * sizeof(EVENTOBJECT); }
""" + validator + r"""
int main(void) {
    SAVED saved;
    size_t size = sizeof(saved) + MAX_EVENT_OBJECTS * sizeof(EVENTOBJECT);
    size_t bytes = sizeof(saved) + 3 * sizeof(EVENTOBJECT);
    memset(&saved, 0, sizeof(saved));
    saved.wNumScene = 93;
    saved.nPartyMember = 2;
    saved.nFollower = 2;  /* The engine supports two follower sprites. */
    saved.rgParty[2].wPlayerRole = 5;
    saved.rgScene[92].wEventObjectIndex = 1;
    saved.rgScene[93].wEventObjectIndex = 3;
    assert(PAL_ValidateSaveData(&saved, size, bytes));
    assert(!PAL_ValidateSaveData(&saved, size, bytes - 1));
    assert(!PAL_ValidateSaveData(&saved, size, bytes + 1));
    saved.nFollower = 3;
    assert(!PAL_ValidateSaveData(&saved, size, bytes));
    saved.nFollower = 2;
    saved.rgParty[2].wPlayerRole = MAX_PLAYER_ROLES;
    assert(!PAL_ValidateSaveData(&saved, size, bytes));
    saved.rgParty[2].wPlayerRole = 5;
    saved.rgScene[93].wEventObjectIndex = 4;
    assert(!PAL_ValidateSaveData(&saved, size, bytes));
    saved.rgScene[93].wEventObjectIndex = 3;
    saved.wNumScene = MAX_SCENES;
    assert(!PAL_ValidateSaveData(&saved, size, bytes));
    saved.wNumScene = 0;
    assert(!PAL_ValidateSaveData(&saved, size, bytes));
    return 0;
}
"""
    harness = tmp_path / "checkpoint_validation.c"
    harness.write_text(source)
    binary = tmp_path / "checkpoint_validation"
    subprocess.run(["cc", "-std=c99", str(harness), "-o", str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)


def test_restore_drops_abandoned_native_events_without_reusing_ids(tmp_path):
    native = (Path(__file__).parents[1] / "vendor/sdlpal/agent.c").read_text()
    reset = c_function(native, "static VOID\nPALPAL_AgentClearCheckpointObservations(")
    source = r"""
#include <assert.h>
#include <wchar.h>
#define VOID void
#define FALSE 0
static struct { int valid, completed, generation; } g_agent_trigger_observation = {1,1,91};
static struct { int valid; } g_agent_latest_dialog_event = {1}, g_agent_last_battle = {1}, g_agent_visible_active = {1};
static struct { int active; } g_agent_choice = {1}, g_agent_shop = {1};
static int g_agent_dialog_event_count = 8, g_agent_dialog_event_next = 8;
static int g_agent_last_dialog_ticks = 1000, g_agent_dialog_speaker_bound = 1;
static int g_agent_visible_outcome_count = 6;
static int g_agent_dialog_sequence = 53;
static wchar_t g_agent_dialog_speaker[20] = L"future speaker";
""" + reset + r"""
int main(void) {
    PALPAL_AgentClearCheckpointObservations();
    assert(!g_agent_trigger_observation.valid && !g_agent_trigger_observation.completed);
    assert(!g_agent_latest_dialog_event.valid && !g_agent_dialog_event_count);
    assert(!g_agent_dialog_event_next && !g_agent_last_dialog_ticks);
    assert(!g_agent_dialog_speaker_bound && !g_agent_dialog_speaker[0]);
    assert(!g_agent_last_battle.valid && !g_agent_visible_outcome_count && !g_agent_visible_active.valid);
    assert(!g_agent_choice.active && !g_agent_shop.active);
    assert(g_agent_trigger_observation.generation == 91);
    assert(g_agent_dialog_sequence == 53);
    return 0;
}
"""
    harness = tmp_path / "checkpoint_events.c"
    harness.write_text(source)
    binary = tmp_path / "checkpoint_events"
    subprocess.run(["cc", "-std=c99", str(harness), "-o", str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
