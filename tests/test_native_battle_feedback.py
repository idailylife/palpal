"""Execute the native collection boundary without starting a game instance."""
import subprocess
from pathlib import Path


def test_native_battle_messages_are_action_scoped_and_bounded(tmp_path):
    native = (Path(__file__).parents[1] / "vendor/sdlpal/agent.c").read_text()
    # Compile the actual observation prefix, before unrelated dialog bookkeeping.
    start = native.index("VOID\nPALPAL_AgentObserveDialogText(")
    end = native.index("   if (g_TextLib.nCurrentDialogLine", start)
    observe = native[start:end] + "}\n"
    start = native.index("static VOID\nPALPAL_AgentCopyWideString(")
    end = native.index("\n}", start) + 2
    copy_string = native[start:end]
    start = native.index("typedef struct tagPALPALAGENTVISIBLEOUTCOME")
    end = native.index("} PALPALAGENTVISIBLEOUTCOME;", start) + len("} PALPALAGENTVISIBLEOUTCOME;")
    outcome = native[start:end]
    harness = tmp_path / "feedback.c"
    harness.write_text(r'''
#include <assert.h>
#include <string.h>
#include <wchar.h>
#include <stddef.h>
typedef void VOID;
typedef int BOOL;
typedef int INT;
typedef unsigned short WORD;
typedef short SHORT;
typedef unsigned int DWORD;
typedef wchar_t WCHAR;
typedef const wchar_t *LPCWSTR;
typedef int PALPALDIALOGEVENT;
#define TRUE 1
#define MAX_PLAYERS_IN_PARTY 3
#define MAX_ENEMIES_IN_TEAM 5
#define MAX_POISONS 16
#define kStatusAll 9
#define PALPAL_AGENT_VISIBLE_NUMBER_CAPACITY 32
#define PALPAL_AGENT_VISIBLE_MESSAGE_CAPACITY 8
static struct { int fInBattle; } globals, *gpGlobals = &globals;
static int enabled = 1;
static int PALPAL_AgentEnabled(void) { return enabled; }
''' + outcome + "\nstatic PALPALAGENTVISIBLEOUTCOME g_agent_visible_active;\n"
        + copy_string + "\n" + observe + r'''
int main(void) {
    const wchar_t *text = L"失敗　沒有效果";
    wchar_t long_text[300];
    globals.fInBattle = 1;
    PALPAL_AgentObserveDialogText(1, 0, text);
    assert(g_agent_visible_active.message_count == 0);
    g_agent_visible_active.valid = 1;
    globals.fInBattle = 0;
    PALPAL_AgentObserveDialogText(1, 0, text);
    assert(g_agent_visible_active.message_count == 0);
    globals.fInBattle = 1;
    PALPAL_AgentObserveDialogText(1, 0, NULL);
    PALPAL_AgentObserveDialogText(1, 0, L"");
    assert(g_agent_visible_active.message_count == 0);
    PALPAL_AgentObserveDialogText(1, 0, text);
    assert(wcscmp(g_agent_visible_active.messages[0], text) == 0);
    for (int i = 1; i < 8; i++) PALPAL_AgentObserveDialogText(i, 0, text);
    assert(!g_agent_visible_active.messages_truncated);
    PALPAL_AgentObserveDialogText(9, 0, text);
    assert(g_agent_visible_active.message_count == 8);
    assert(g_agent_visible_active.messages_truncated);
    memset(&g_agent_visible_active, 0, sizeof(g_agent_visible_active));
    g_agent_visible_active.valid = 1;
    for (int i = 0; i < 299; i++) long_text[i] = L'x';
    long_text[299] = 0;
    PALPAL_AgentObserveDialogText(1, 0, long_text);
    assert(g_agent_visible_active.message_count == 1);
    assert(wcslen(g_agent_visible_active.messages[0]) == 255);
    assert(g_agent_visible_active.messages_truncated);
    return 0;
}
''')
    binary = tmp_path / "feedback"
    subprocess.run(["cc", "-std=c99", str(harness), "-o", str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
