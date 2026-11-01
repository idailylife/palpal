"""Exercise production room flood fill when a scripted landing is blocked."""
from pathlib import Path
import subprocess


def test_blocked_landing_preserves_native_step_lattice(tmp_path):
    native = (Path(__file__).parents[1] / 'vendor/sdlpal/agent.c').read_text()
    start = native.index('#define PALPAL_ROOM_GRID_W')
    end = native.index('static VOID\nPALPAL_AgentWriteRoom(', start)
    source = r'''
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#define VOID void
#define PAL_XY(x,y) ((uint32_t)(x) | ((uint32_t)(y) << 16))
static int mode;
static BOOL PAL_CheckObstacleWithRange(uint32_t pos, BOOL objects, int self, BOOL range) {
    int x = (pos & 65535) / 16, y = (pos >> 16) / 8;
    (void)objects; (void)self; (void)range;
    if (mode == 2) return TRUE;
    if (mode == 3) return (x + y) % 2 == 0;
    if (mode == 1) return FALSE;
    /* Scripted landing (1504,624): old scan selected (95,78),
       although an actual native step can reach (95,79). */
    return !((x == 95 && y == 78) || (x == 95 && y == 79) ||
             (x == 96 && y == 80));
}
''' + native[start:end] + r'''
int main(void) {
    int x = 94, y = 78;
    PALPALROOMINFO room;
    assert(PALPAL_AgentFindRoomStart(&x, &y));
    assert(x == 95 && y == 79);
    room = PALPAL_AgentComputeRoom(1504,624);
    assert(room.count == 2);
    assert(!g_agent_room_seen[PALPAL_AgentRoomIndex(94,78)]);
    assert(!g_agent_room_seen[PALPAL_AgentRoomIndex(95,78)]);
    assert(g_agent_room_seen[PALPAL_AgentRoomIndex(95,79)]);
    assert(g_agent_room_seen[PALPAL_AgentRoomIndex(96,80)]);
    mode = 1;
    x = 94; y = 78;
    assert(PALPAL_AgentFindRoomStart(&x, &y) && x == 94 && y == 78);
    room = PALPAL_AgentComputeRoom(1504,624);
    assert(room.count > 0);
    for (int i = 0; i < PALPAL_ROOM_GRID_SIZE; i++) {
        if (g_agent_room_seen[i])
            assert(((i % PALPAL_ROOM_GRID_W) + (i / PALPAL_ROOM_GRID_W)) % 2 == 0);
    }
    mode = 2;
    assert(PALPAL_AgentComputeRoom(1504,624).count == 0);
    mode = 3;
    /* Plenty of nearby cells, but none on the player's step lattice. */
    assert(PALPAL_AgentComputeRoom(1504,624).count == 0);
    return 0;
}
'''
    harness = tmp_path / 'navigation_grid.c'
    harness.write_text(source)
    binary = tmp_path / 'navigation_grid'
    subprocess.run(['cc', '-std=c99', str(harness), '-o', str(binary)],
                   check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
