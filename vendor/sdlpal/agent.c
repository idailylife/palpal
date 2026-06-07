/* Agent bridge for PALPAL automation.
 *
 * This file intentionally uses a small file protocol instead of embedding a
 * web server in SDLPAL. It keeps the game loop patch narrow while still
 * exposing live state to external tools.
 */

#include "main.h"
#include "agent.h"
#include "battle.h"
#include "map.h"

#include <errno.h>

static int g_agent_initialized = 0;
static int g_agent_enabled = 0;
static char g_agent_runtime_dir[PAL_MAX_PATH] = "runtime";
static DWORD g_agent_buttons = 0;
static PALDIRECTION g_agent_dir = kDirUnknown;
static int g_agent_hold_frames = 0;

static VOID
PALPAL_AgentInit(
   VOID
)
{
   const char *enabled;
   const char *runtime_dir;

   if (g_agent_initialized)
   {
      return;
   }

   g_agent_initialized = 1;
   enabled = getenv("PALPAL_AGENT");
   g_agent_enabled = (enabled && strcmp(enabled, "0") != 0);

   runtime_dir = getenv("PALPAL_RUNTIME_DIR");
   if (runtime_dir && runtime_dir[0])
   {
      strncpy(g_agent_runtime_dir, runtime_dir, sizeof(g_agent_runtime_dir) - 1);
      g_agent_runtime_dir[sizeof(g_agent_runtime_dir) - 1] = '\0';
   }
}

BOOL
PALPAL_AgentEnabled(
   VOID
)
{
   PALPAL_AgentInit();
   return g_agent_enabled ? TRUE : FALSE;
}

INT
PALPAL_AgentSaveSlot(
   VOID
)
{
   const char *slot = getenv("PALPAL_SAVE_SLOT");
   if (!slot || !slot[0])
   {
      return 0;
   }
   return atoi(slot);
}

static VOID
PALPAL_AgentPath(
   char *buffer,
   size_t size,
   const char *filename
)
{
   snprintf(buffer, size, "%s/%s", g_agent_runtime_dir, filename);
}

static DWORD
PALPAL_AgentButtonForName(
   const char *name
)
{
   if (strcmp(name, "menu") == 0) return kKeyMenu;
   if (strcmp(name, "search") == 0) return kKeySearch;
   if (strcmp(name, "down") == 0) return kKeyDown;
   if (strcmp(name, "left") == 0) return kKeyLeft;
   if (strcmp(name, "up") == 0) return kKeyUp;
   if (strcmp(name, "right") == 0) return kKeyRight;
   if (strcmp(name, "page_up") == 0) return kKeyPgUp;
   if (strcmp(name, "page_down") == 0) return kKeyPgDn;
   if (strcmp(name, "repeat") == 0) return kKeyRepeat;
   if (strcmp(name, "auto") == 0) return kKeyAuto;
   if (strcmp(name, "defend") == 0) return kKeyDefend;
   if (strcmp(name, "use_item") == 0) return kKeyUseItem;
   if (strcmp(name, "throw_item") == 0) return kKeyThrowItem;
   if (strcmp(name, "flee") == 0) return kKeyFlee;
   if (strcmp(name, "status") == 0) return kKeyStatus;
   if (strcmp(name, "force") == 0) return kKeyForce;
   if (strcmp(name, "home") == 0) return kKeyHome;
   if (strcmp(name, "end") == 0) return kKeyEnd;
   return kKeyNone;
}

static VOID
PALPAL_AgentAddButton(
   const char *name
)
{
   DWORD button = PALPAL_AgentButtonForName(name);
   g_agent_buttons |= button;

   if (button & kKeyUp) g_agent_dir = kDirNorth;
   else if (button & kKeyDown) g_agent_dir = kDirSouth;
   else if (button & kKeyLeft) g_agent_dir = kDirWest;
   else if (button & kKeyRight) g_agent_dir = kDirEast;
}

static VOID
PALPAL_AgentLoadActionFile(
   VOID
)
{
   char path[PAL_MAX_PATH];
   FILE *fp;
   char line[128];

   PALPAL_AgentPath(path, sizeof(path), "action.txt");
   fp = fopen(path, "r");
   if (!fp)
   {
      return;
   }

   g_agent_buttons = 0;
   g_agent_dir = kDirUnknown;
   g_agent_hold_frames = 1;

   while (fgets(line, sizeof(line), fp))
   {
      char key[32], value[64];
      if (sscanf(line, "%31s %63s", key, value) != 2)
      {
         continue;
      }
      if (strcmp(key, "frames") == 0)
      {
         g_agent_hold_frames = atoi(value);
         if (g_agent_hold_frames < 1)
         {
            g_agent_hold_frames = 1;
         }
      }
      else if (strcmp(key, "button") == 0)
      {
         PALPAL_AgentAddButton(value);
      }
   }

   fclose(fp);
   remove(path);
}

VOID
PALPAL_AgentPollAction(
   VOID
)
{
   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   if (g_agent_hold_frames <= 0)
   {
      PALPAL_AgentLoadActionFile();
   }

   if (g_agent_hold_frames > 0)
   {
      g_InputState.dwKeyPress |= g_agent_buttons;
      g_InputState.dir = g_agent_dir;
      g_InputState.prevdir = kDirUnknown;
      g_agent_hold_frames--;
   }
}

static INT
PALPAL_PosX(
   PAL_POS pos
)
{
   return PAL_X(pos);
}

static INT
PALPAL_PosY(
   PAL_POS pos
)
{
   return PAL_Y(pos);
}

static VOID
PALPAL_AgentWriteInventory(
   FILE *fp
)
{
   int i;
   int first = 1;

   fprintf(fp, "\"inventory\":[");
   for (i = 0; i < MAX_INVENTORY; i++)
   {
      if (gpGlobals->rgInventory[i].wItem == 0 || gpGlobals->rgInventory[i].nAmount == 0)
      {
         continue;
      }
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"slot\":%d,\"item_id\":%u,\"amount\":%d,\"amount_in_use\":%d}",
         i,
         gpGlobals->rgInventory[i].wItem,
         gpGlobals->rgInventory[i].nAmount,
         gpGlobals->rgInventory[i].nAmountInUse);
      first = 0;
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteParty(
   FILE *fp
)
{
   int i;

   fprintf(fp, "\"party\":[");
   for (i = 0; i <= (int)gpGlobals->wMaxPartyMemberIndex; i++)
   {
      WORD role = gpGlobals->rgParty[i].wPlayerRole;
      if (i > 0) fprintf(fp, ",");
      fprintf(fp,
         "{\"index\":%d,\"role_id\":%u,\"level\":%u,\"hp\":%u,\"max_hp\":%u,"
         "\"mp\":%u,\"max_mp\":%u,\"x\":%d,\"y\":%d,\"frame\":%u}",
         i,
         role,
         gpGlobals->g.PlayerRoles.rgwLevel[role],
         gpGlobals->g.PlayerRoles.rgwHP[role],
         gpGlobals->g.PlayerRoles.rgwMaxHP[role],
         gpGlobals->g.PlayerRoles.rgwMP[role],
         gpGlobals->g.PlayerRoles.rgwMaxMP[role],
         gpGlobals->rgParty[i].x,
         gpGlobals->rgParty[i].y,
         gpGlobals->rgParty[i].wFrame);
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteObjects(
   FILE *fp
)
{
   int i, start, end;
   int first = 1;

   fprintf(fp, "\"objects\":[");
   if (gpGlobals->wNumScene > 0 && gpGlobals->wNumScene < MAX_SCENES)
   {
      start = gpGlobals->g.rgScene[gpGlobals->wNumScene - 1].wEventObjectIndex;
      end = gpGlobals->g.rgScene[gpGlobals->wNumScene].wEventObjectIndex;
      for (i = start; i < end; i++)
      {
         LPEVENTOBJECT obj = &gpGlobals->g.lprgEventObject[i];
         if (!first) fprintf(fp, ",");
         fprintf(fp,
            "{\"id\":%d,\"x\":%d,\"y\":%d,\"sprite_id\":%u,\"state\":%d,"
            "\"trigger_mode\":%u,\"trigger_script\":%u,\"auto_script\":%u,"
            "\"blocking\":%s,\"interactable\":%s,\"vanish_time\":%d}",
            i + 1,
            obj->x,
            obj->y,
            obj->wSpriteNum,
            obj->sState,
            obj->wTriggerMode,
            obj->wTriggerScript,
            obj->wAutoScript,
            obj->sState >= kObjStateBlocker ? "true" : "false",
            (obj->sState > 0 && obj->wTriggerScript != 0) ? "true" : "false",
            obj->sVanishTime);
         first = 0;
      }
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteWalkability(
   FILE *fp,
   int player_x,
   int player_y
)
{
   int tx = player_x / 32;
   int ty = player_y / 16;
   int x, y, first = 1;
   LPCPALMAP map = PAL_GetCurrentMap();

   fprintf(fp, "\"blocked_tiles\":[");
   if (map)
   {
      for (y = ty - 6; y <= ty + 6; y++)
      {
         for (x = tx - 6; x <= tx + 6; x++)
         {
            if (x < 0 || x >= 64 || y < 0 || y >= 128)
            {
               continue;
            }
            if (PAL_MapTileIsBlocked((BYTE)x, (BYTE)y, 0, map) ||
                PAL_MapTileIsBlocked((BYTE)x, (BYTE)y, 1, map))
            {
               if (!first) fprintf(fp, ",");
               fprintf(fp, "{\"x\":%d,\"y\":%d}", x, y);
               first = 0;
            }
         }
      }
   }
   fprintf(fp, "]");
}

VOID
PALPAL_AgentWriteState(
   VOID
)
{
   char state_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   char screen_path[PAL_MAX_PATH];
   FILE *fp;
   int scene_index;
   WORD map_id = 0;
   int player_x;
   int player_y;

   if (!PALPAL_AgentEnabled() || !gpGlobals->fInMainGame)
   {
      return;
   }

   scene_index = gpGlobals->wNumScene > 0 ? gpGlobals->wNumScene - 1 : 0;
   if (scene_index >= 0 && scene_index < MAX_SCENES)
   {
      map_id = gpGlobals->g.rgScene[scene_index].wMapNum;
   }

   player_x = PALPAL_PosX(gpGlobals->viewport) + PALPAL_PosX(gpGlobals->partyoffset);
   player_y = PALPAL_PosY(gpGlobals->viewport) + PALPAL_PosY(gpGlobals->partyoffset);

   PALPAL_AgentPath(state_path, sizeof(state_path), "state.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "state.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp, "{");
   fprintf(fp, "\"frame\":%u,", (unsigned int)gpGlobals->dwFrameNum);
   fprintf(fp, "\"in_main_game\":%s,", gpGlobals->fInMainGame ? "true" : "false");
   fprintf(fp, "\"in_battle\":%s,", gpGlobals->fInBattle ? "true" : "false");
   fprintf(fp, "\"entering_scene\":%s,", gpGlobals->fEnteringScene ? "true" : "false");
   fprintf(fp, "\"scene_id\":%u,\"map_id\":%u,", gpGlobals->wNumScene, map_id);
   fprintf(fp, "\"cash\":%u,", (unsigned int)gpGlobals->dwCash);
   fprintf(fp, "\"viewport\":{\"x\":%d,\"y\":%d},",
      PALPAL_PosX(gpGlobals->viewport), PALPAL_PosY(gpGlobals->viewport));
   fprintf(fp, "\"party_offset\":{\"x\":%d,\"y\":%d},",
      PALPAL_PosX(gpGlobals->partyoffset), PALPAL_PosY(gpGlobals->partyoffset));
   fprintf(fp, "\"party_position\":{\"x\":%d,\"y\":%d},", player_x, player_y);
   fprintf(fp, "\"party_direction\":%u,", gpGlobals->wPartyDirection);
   fprintf(fp, "\"region_key\":\"%u:%u:%d:%d\",",
      gpGlobals->wNumScene, map_id, player_x / 64, player_y / 32);

   PALPAL_AgentWriteParty(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteInventory(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteObjects(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteWalkability(fp, player_x, player_y);
   fprintf(fp, "}\n");

   fclose(fp);
   rename(tmp_path, state_path);

   PALPAL_AgentPath(screen_path, sizeof(screen_path), "screen.bmp");
   if (gpScreen)
   {
      char error_path[PAL_MAX_PATH];
      PALPAL_AgentPath(error_path, sizeof(error_path), "screen_error.txt");
      if (PAL_GetCurrentMap())
      {
         PAL_MakeScene();
      }
#if SDL_VERSION_ATLEAST(3,0,0)
      if (!SDL_SaveBMP(gpScreen, screen_path))
#else
      if (SDL_SaveBMP(gpScreen, screen_path) != 0)
#endif
      {
         FILE *efp = fopen(error_path, "w");
         if (efp)
         {
            fprintf(efp, "%s\n", SDL_GetError());
            fclose(efp);
         }
      }
      else
      {
         remove(error_path);
      }
   }
   else
   {
      char error_path[PAL_MAX_PATH];
      FILE *efp;
      PALPAL_AgentPath(error_path, sizeof(error_path), "screen_error.txt");
      efp = fopen(error_path, "w");
      if (efp)
      {
         fprintf(efp, "gpScreen is NULL\n");
         fclose(efp);
      }
   }
}
