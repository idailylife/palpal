"""Exercise the native item-menu loop with deterministic input and clock stubs."""

from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="module")
def item_menu_harness(tmp_path_factory):
    source = (Path(__file__).resolve().parents[1] / "vendor/sdlpal/itemmenu.c").read_text()
    # Compile the production loop, replacing only its engine dependencies.
    loop = source[source.index("WORD\nPAL_ItemSelectMenu("):]
    directory = tmp_path_factory.mktemp("native-item-menu")
    harness = directory / "item_menu.c"
    harness.write_text(r"""
#include <assert.h>
#include <stdlib.h>
typedef unsigned short WORD;
typedef unsigned int DWORD;
typedef void VOID;
typedef void (*LPITEMCHANGED_CALLBACK)(WORD);
#define TRUE 1
#define FALSE 0
#define MAX_INVENTORY 2
#define FRAME_TIME 60
#define SDL_TICKS_PASSED(a, b) ((a) >= (b))
static struct {
    int iCurInvMenuItem;
    struct { WORD wItem; } rgInventory[MAX_INVENTORY];
} globals = {0, {{91}, {168}}}, *gpGlobals = &globals;
static struct { DWORD dwKeyPress; } g_InputState;
static int g_fNoDesc, polls, callbacks, cancel_menu;
static DWORD ticks, last_input;
enum { RIGHT = 1, SEARCH = 2, DOWN = 4, MENU = 8 };
static DWORD buttons[3] = {RIGHT, SEARCH, DOWN};
static void PAL_ItemSelectMenuInit(WORD flags) { (void)flags; }
static void PAL_ClearKeyState(void) { g_InputState.dwKeyPress = 0; }
static void PAL_MakeScene(void) {}
static void VIDEO_UpdateScreen(void *rect) { (void)rect; }
static DWORD SDL_GetTicks(void) { return ticks; }
static void SDL_Delay(int delay) { ticks += delay; }
static void PAL_ProcessEvent(void) {
    if (polls && ticks - last_input < 30) return;
    assert(polls < 3);
    last_input = ticks;
    g_InputState.dwKeyPress |= buttons[polls++];
}
static WORD PAL_ItemSelectMenuUpdate(void) {
    if (g_InputState.dwKeyPress & MENU) return 0;
    if (g_InputState.dwKeyPress & SEARCH)
        return gpGlobals->rgInventory[gpGlobals->iCurInvMenuItem].wItem;
    if (g_InputState.dwKeyPress & RIGHT) gpGlobals->iCurInvMenuItem = 1;
    return 0xFFFF;
}
static void changed(WORD item) {
    assert(item == (callbacks == 0 ? 91 : 168));
    callbacks++;
}
""" + loop + r"""
int main(int argc, char **argv) {
    assert(argc == 2);
    cancel_menu = atoi(argv[1]);
    if (cancel_menu) buttons[1] = MENU;
    assert(PAL_ItemSelectMenu(changed, 1) == (cancel_menu ? 0 : 168));
    assert(!g_fNoDesc);
    assert(g_InputState.dwKeyPress == 0);
    assert(callbacks == 2);
    /* The following menu must receive DOWN, not have it cleared on entry. */
    assert(polls == 2);
    PAL_ClearKeyState();
    ticks += 30;
    PAL_ProcessEvent();
    assert(g_InputState.dwKeyPress == DOWN);
    return 0;
}
""")
    executable = directory / "item_menu"
    subprocess.run(["cc", "-std=c99", "-Wall", "-Wextra", "-Werror",
                    str(harness), "-o", str(executable)], check=True, capture_output=True)
    return executable


@pytest.mark.parametrize("cancel", [False, True], ids=["selected", "cancelled"])
def test_item_menu_preserves_next_menu_input(item_menu_harness, cancel):
    subprocess.run([str(item_menu_harness), str(int(cancel))], check=True, capture_output=True)
