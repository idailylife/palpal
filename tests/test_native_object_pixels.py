"""Run the production RLE blitter and provenance hook, without SDL or assets."""
from pathlib import Path
import subprocess


def c_function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def test_native_visible_pixels_follow_transparency_clipping_and_draw_order(tmp_path):
    root = Path(__file__).resolve().parents[1] / "vendor/sdlpal"
    blitter = c_function((root / "palcommon.c").read_text(), "INT\nPAL_RLEBlitToSurfaceWithShadow(")
    hook = c_function((root / "agent.c").read_text(), "VOID PALPAL_AgentDrawSpan(")
    source = r"""
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <stddef.h>
typedef unsigned int UINT;
typedef int INT, BOOL;
typedef unsigned char BYTE, *LPBYTE;
typedef const BYTE *LPCBITMAPRLE;
typedef uint16_t WORD;
typedef uint32_t PAL_POS;
typedef void VOID;
typedef struct { int w, h, pitch; void *pixels; } SDL_Surface;
#define PAL_X(p) ((int16_t)((p) & 0xffff))
#define PAL_Y(p) ((int16_t)((p) >> 16))
#define XY(x,y) ((PAL_POS)(uint16_t)(x) | ((PAL_POS)(uint16_t)(y) << 16))
static WORD g_agent_object_mask[320 * 200];
static UINT g_agent_object_drawn[65536];
static BOOL g_agent_object_drawing = 1;
static WORD g_agent_drawing_object = 797;
static SDL_Surface *gpScreen;
static BYTE PAL_CalcShadowColor(BYTE value) { return value / 2; }
""" + hook + "\n" + blitter + r"""
int main(void) {
    BYTE pixels[16] = {0};
    SDL_Surface surface = {8, 2, 8, pixels};
    BYTE solid[] = {4,0,1,0,4,11,12,13,14};
    BYTE cover[] = {4,0,1,0,0x81,2,8,9,0x81};
    gpScreen = &surface;
    assert(PAL_RLEBlitToSurfaceWithShadow(solid, gpScreen, XY(1,0), 0) == 0);
    assert(g_agent_object_drawn[797] == 4);
    assert(g_agent_object_mask[1] == 797 && g_agent_object_mask[4] == 797);
    /* A later map tile covers only its nontransparent pixels. */
    g_agent_drawing_object = 0;
    PAL_RLEBlitToSurfaceWithShadow(cover, gpScreen, XY(1,0), 0);
    assert(g_agent_object_mask[1] == 797 && g_agent_object_mask[4] == 797);
    assert(g_agent_object_mask[2] == 0 && g_agent_object_mask[3] == 0);
    /* A later object owns only pixels it actually drew on screen. */
    g_agent_drawing_object = 798;
    PAL_RLEBlitToSurfaceWithShadow(solid, gpScreen, XY(-2,1), 0);
    assert(g_agent_object_drawn[798] == 2);
    assert(g_agent_object_mask[320] == 798 && g_agent_object_mask[321] == 798);
    assert(g_agent_object_mask[322] == 0);
    PAL_RLEBlitToSurfaceWithShadow(solid, gpScreen, XY(20,1), 0);
    assert(g_agent_object_drawn[798] == 2);
    /* Subsequent UI drawing cannot claim object pixels. */
    g_agent_object_drawing = 0;
    PAL_RLEBlitToSurfaceWithShadow(solid, gpScreen, XY(1,0), 0);
    assert(g_agent_object_mask[2] == 0);
    return 0;
}
"""
    harness = tmp_path / "pixels.c"
    harness.write_text(source)
    binary = tmp_path / "pixels"
    compiled = subprocess.run(["cc", "-std=c99", str(harness), "-o", str(binary)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
