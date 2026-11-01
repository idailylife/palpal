/* Agent bridge for PALPAL automation.
 *
 * Realtime state and frames use a small local Unix socket protocol. Sparse
 * actions and persistent companion data remain file-based so the SDLPAL patch
 * stays narrow and wait-loop input polling remains reliable.
 */

#include "main.h"
#include "agent.h"
#include "battle.h"
#include "map.h"
#include "text.h"

#include <errno.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

static int g_agent_initialized = 0;
static int g_agent_enabled = 0;
static int g_agent_headless = 0;
static BOOL g_agent_checkpoint_paused = FALSE;
static BOOL g_agent_checkpoint_loading = FALSE;
static BOOL g_agent_checkpoint_failed = FALSE;
static char g_agent_slot_request_id[33] = "";
static char g_agent_loading_request_id[33] = "";
static char g_agent_loading_action[16] = "";
static int g_agent_loading_slot = 0;

static BOOL g_agent_startup_waiting = FALSE;
static BOOL g_agent_startup_ready = FALSE;
static int g_agent_startup_slot = 0;
static char g_agent_runtime_dir[PAL_MAX_PATH] = "runtime";
static char g_agent_socket_path[PAL_MAX_PATH];
static char g_agent_game_instance_id[65];
static PALDIRECTION g_agent_injected_dir = kDirUnknown;
static BOOL g_agent_action_pending = FALSE;
static BOOL g_agent_dir_active = FALSE;
static DWORD g_agent_last_dialog_ticks = 0;
static DWORD g_agent_dialog_sequence = 0;
static WCHAR g_agent_dialog_speaker[64];
static BOOL g_agent_dialog_speaker_bound = FALSE;
static WORD g_agent_dialog_speaker_scene_id = 0;
static WORD g_agent_dialog_speaker_map_id = 0;
static WORD g_agent_dialog_speaker_event_object_id = 0;

typedef struct tagPALPALAGENTTRIGGEROBSERVATION
{
   BOOL valid;
   BOOL completed;
   DWORD generation;
   WORD event_object_id;
   WORD trigger_script;
   WORD source_scene_id;
   WORD source_map_id;
   int source_x;
   int source_y;
   WORD destination_scene_id;
   WORD destination_map_id;
   int destination_x;
   int destination_y;
   DWORD destination_frame;
} PALPALAGENTTRIGGEROBSERVATION;

static PALPALAGENTTRIGGEROBSERVATION g_agent_trigger_observation;

#define PALPAL_AGENT_MAX_ACTION_STEPS 64
#define PALPAL_AGENT_DIALOG_EVENT_CAPACITY 32
#define PALPAL_AGENT_OBJECT_DESC_CHARS 512
#define PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY 32
#define PALPAL_AGENT_VISIBLE_NUMBER_CAPACITY 32
#define PALPAL_AGENT_VISIBLE_MESSAGE_CAPACITY 8

typedef struct tagPALPALAGENTOBJECTDESC
{
   BOOL resolved;
   WCHAR text[PALPAL_AGENT_OBJECT_DESC_CHARS];
} PALPALAGENTOBJECTDESC;

static PALPALAGENTOBJECTDESC g_agent_magic_desc[MAX_OBJECTS];
static PALPALAGENTOBJECTDESC g_agent_item_desc[MAX_OBJECTS];

static LPCWSTR PALPAL_AgentMagicDescription(WORD object_id);
static LPCWSTR PALPAL_AgentItemDescription(WORD object_id);
static VOID PALPAL_AgentWriteStatusEffects(FILE *fp, WORD *values);
static VOID PALPAL_AgentWritePlayerPoisons(FILE *fp, WORD party_index);

typedef struct tagPALPALACTIONSTEP
{
   DWORD buttons;
   PALDIRECTION dir;
} PALPALACTIONSTEP;

static PALPALACTIONSTEP g_agent_action_queue[PALPAL_AGENT_MAX_ACTION_STEPS];
static int g_agent_action_count = 0;
static int g_agent_action_index = 0;
static DWORD g_agent_action_generation = 0;
static DWORD g_agent_last_action_ticks = 0;
static BOOL g_agent_waiting_for_move = FALSE;
static int g_agent_wait_x = 0;
static int g_agent_wait_y = 0;
static DWORD g_agent_wait_ticks = 0;

static VOID PALPAL_AgentClearActionQueue(VOID);

typedef enum tagPALPALAGENTSHOPPHASE
{
   kPalpalAgentShopPhaseSelect = 0,
   kPalpalAgentShopPhaseConfirm
} PALPALAGENTSHOPPHASE;

typedef enum tagPALPALAGENTSHOPMODE
{
   kPalpalAgentShopModeBuy = 0,
   kPalpalAgentShopModeSell
} PALPALAGENTSHOPMODE;

typedef enum tagPALPALAGENTSHOPCOMMAND
{
   kPalpalAgentShopCommandNone = 0,
   kPalpalAgentShopCommandBuy,
   kPalpalAgentShopCommandSell,
   kPalpalAgentShopCommandLeave
} PALPALAGENTSHOPCOMMAND;

typedef struct tagPALPALAGENTSHOP
{
   BOOL active;
   PALPALAGENTSHOPMODE mode;
   WORD store_id;
   WORD selected_item_id;
   int selected_index;
   int item_count;
   PALPALAGENTSHOPPHASE phase;
   PALPALAGENTSHOPCOMMAND command;
   WORD command_item_id;
} PALPALAGENTSHOP;

static PALPALAGENTSHOP g_agent_shop;

typedef enum tagPALPALAGENTBATTLEPHASE
{
   kPalpalAgentBattleNone = 0,
   kPalpalAgentBattlePreBattle,
   kPalpalAgentBattleResolving,
   kPalpalAgentBattleCommand,
   kPalpalAgentBattleReward,
   kPalpalAgentBattleEnding
} PALPALAGENTBATTLEPHASE;

typedef struct tagPALPALAGENTBATTLE
{
   DWORD generation;
   DWORD command_generation;
   BOOL hp_tracking_safe;
   PALPALAGENTBATTLEPHASE phase;
   BOOL awaiting_ack;
   WORD current_player_index;
   WORD enemy_object_id[MAX_ENEMIES_IN_TEAM];
   WORD enemy_max_hp[MAX_ENEMIES_IN_TEAM];
   WORD player_role[MAX_PLAYERS_IN_PARTY];
   WORD player_hp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_level_before[MAX_PLAYERS_IN_PARTY];
   WORD player_status_before[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_status_after[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_magics_before[MAX_PLAYERS_IN_PARTY][MAX_PLAYER_MAGICS];
   WORD player_new_magics[MAX_PLAYERS_IN_PARTY][MAX_PLAYER_MAGICS];
   DWORD started_ticks;
} PALPALAGENTBATTLE;

typedef struct tagPALPALAGENTVISIBLEOUTCOME
{
   BOOL valid;
   DWORD sequence;
   BOOL player_actor;
   BOOL round_end;
   WORD actor_index;
   WORD actor_role;
   INT action_type;
   WORD object_id;
   SHORT target_index;
   WORD target_object_id;
   INT mp_cost;
   BOOL enemy_target;
   WCHAR messages[PALPAL_AGENT_VISIBLE_MESSAGE_CAPACITY][256];
   int message_count;
   BOOL messages_truncated;
   WORD player_hp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_hp_after[MAX_PLAYERS_IN_PARTY];
   WORD player_hp_max[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_after[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_max[MAX_PLAYERS_IN_PARTY];
   WORD player_status_before[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_status_after[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_poison_before[MAX_PLAYERS_IN_PARTY][MAX_POISONS];
   WORD player_poison_after[MAX_PLAYERS_IN_PARTY][MAX_POISONS];
   WORD enemy_object_id[MAX_ENEMIES_IN_TEAM];
   WORD enemy_hp_before[MAX_ENEMIES_IN_TEAM];
   WORD enemy_hp_after[MAX_ENEMIES_IN_TEAM];
   struct {
      BOOL enemy;
      WORD target_index;
      BOOL mp;
      SHORT change;
   } numbers[PALPAL_AGENT_VISIBLE_NUMBER_CAPACITY];
   int number_count;
} PALPALAGENTVISIBLEOUTCOME;

typedef struct tagPALPALAGENTLASTBATTLE
{
   BOOL valid;
   BOOL hp_tracking_safe;
   DWORD generation;
   int result;
   BOOL is_boss;
   DWORD ended_frame;
   DWORD duration_ms;
   int exp_gained;
   int cash_gained;
   WORD enemy_object_id[MAX_ENEMIES_IN_TEAM];
   WORD player_role[MAX_PLAYERS_IN_PARTY];
   WORD player_hp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_before[MAX_PLAYERS_IN_PARTY];
   WORD player_level_before[MAX_PLAYERS_IN_PARTY];
   WORD player_hp_after[MAX_PLAYERS_IN_PARTY];
   WORD player_mp_after[MAX_PLAYERS_IN_PARTY];
   WORD player_level_after[MAX_PLAYERS_IN_PARTY];
   WORD player_status_before[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_status_after[MAX_PLAYERS_IN_PARTY][kStatusAll];
   WORD player_new_magics[MAX_PLAYERS_IN_PARTY][MAX_PLAYER_MAGICS];
   PALPALAGENTVISIBLEOUTCOME visible_outcomes[PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY];
   int visible_outcome_count;
} PALPALAGENTLASTBATTLE;

static PALPALAGENTBATTLE g_agent_battle;
static PALPALAGENTLASTBATTLE g_agent_last_battle;
static PALPALAGENTVISIBLEOUTCOME g_agent_visible_outcomes[PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY];
static PALPALAGENTVISIBLEOUTCOME g_agent_visible_active;
static int g_agent_visible_outcome_count = 0;
static DWORD g_agent_visible_sequence = 0;

typedef enum tagPALPALAGENTSLOTCOMMAND
{
   kPalpalAgentSlotCommandNone = 0,
   kPalpalAgentSlotCommandSave,
   kPalpalAgentSlotCommandLoad,
   kPalpalAgentSlotCommandStart
} PALPALAGENTSLOTCOMMAND;

static const char * const g_agent_equipment_slot_keys[MAX_PLAYER_EQUIPMENTS] = {
   "head",
   "body",
   "shoulder",
   "hand",
   "feet",
   "wear"
};

#define PALPAL_AGENT_EQUIP_SCRIPT_LIMIT 64
#define PALPAL_AGENT_EQUIP_SPECIAL_LIMIT 8
#define PALPAL_AGENT_EQUIP_UNKNOWN_LIMIT 8
#define PALPAL_AGENT_ITEM_SCRIPT_LIMIT 64
#define PALPAL_AGENT_ITEM_EFFECT_LIMIT 16
#define PALPAL_AGENT_ITEM_REQUIREMENT_LIMIT 8
#define PALPAL_AGENT_ITEM_UNKNOWN_LIMIT 8

typedef struct tagPALPALEQUIPMENTSPECIAL
{
   WORD operation;
   WORD operand0;
   WORD operand1;
   WORD operand2;
} PALPALEQUIPMENTSPECIAL;

typedef struct tagPALPALEQUIPMENTPREVIEW
{
   BOOL complete;
   int slot_id;
   SHORT attack_strength;
   SHORT magic_strength;
   SHORT defense;
   SHORT dexterity;
   SHORT flee_rate;
   SHORT poison_resistance;
   SHORT elemental_resistance[NUM_MAGIC_ELEMENTAL];
   BOOL has_battle_sprite;
   WORD battle_sprite;
   BOOL has_attack_all;
   WORD attack_all;
   BOOL has_cooperative_magic;
   WORD cooperative_magic;
   PALPALEQUIPMENTSPECIAL specials[PALPAL_AGENT_EQUIP_SPECIAL_LIMIT];
   int special_count;
   PALPALEQUIPMENTSPECIAL unknown[PALPAL_AGENT_EQUIP_UNKNOWN_LIMIT];
   int unknown_count;
} PALPALEQUIPMENTPREVIEW;

typedef enum tagPALPALITEMEFFECTKIND
{
   kPalpalItemEffectRestoreHP = 0,
   kPalpalItemEffectRestoreMP,
   kPalpalItemEffectRestoreHPMP,
   kPalpalItemEffectRevive,
   kPalpalItemEffectApplyPoison,
   kPalpalItemEffectCurePoison,
   kPalpalItemEffectCurePoisonLevel,
   kPalpalItemEffectApplyStatus,
   kPalpalItemEffectRemoveStatus,
   kPalpalItemEffectEnemyDamage,
   kPalpalItemEffectDrainHP
} PALPALITEMEFFECTKIND;

typedef struct tagPALPALITEMEFFECT
{
   PALPALITEMEFFECTKIND kind;
   WORD script_entry;
   WORD operand0;
   WORD operand1;
   WORD operand2;
   BOOL conditional;
} PALPALITEMEFFECT;

typedef struct tagPALPALITEMREQUIREMENT
{
   enum
   {
      kPalpalItemRequirementFaceEventObject = 0,
      kPalpalItemRequirementPlaceEventObject
   } kind;
   WORD script_entry;
   WORD event_object_id;
   WORD distance_level;
   WORD failure_script_entry;
   BOOL conditional;
} PALPALITEMREQUIREMENT;

typedef struct tagPALPALITEMEFFECTPREVIEW
{
   BOOL complete;
   BOOL requirements_complete;
   WORD script_entry;
   PALPALITEMEFFECT effects[PALPAL_AGENT_ITEM_EFFECT_LIMIT];
   int effect_count;
   PALPALITEMREQUIREMENT requirements[PALPAL_AGENT_ITEM_REQUIREMENT_LIMIT];
   int requirement_count;
   PALPALEQUIPMENTSPECIAL unknown[PALPAL_AGENT_ITEM_UNKNOWN_LIMIT];
   WORD unknown_entries[PALPAL_AGENT_ITEM_UNKNOWN_LIMIT];
   int unknown_count;
} PALPALITEMEFFECTPREVIEW;

typedef struct tagPALPALITEMPREVIEWCACHE
{
   BOOL valid;
   WORD use_script_entry;
   WORD throw_script_entry;
   PALPALITEMEFFECTPREVIEW use_preview;
   PALPALITEMEFFECTPREVIEW throw_preview;
} PALPALITEMPREVIEWCACHE;

static PALPALITEMPREVIEWCACHE g_agent_item_preview_cache[MAX_OBJECTS];

typedef struct tagPALPALDIALOGEVENT
{
   BOOL valid;
   DWORD sequence;
   DWORD trigger_generation;
   DWORD frame;
   WORD scene_id;
   WORD map_id;
   WORD event_object_id;
   INT message_id;
   BYTE position;
   WCHAR speaker[64];
   WCHAR text[512];
} PALPALDIALOGEVENT;

static PALPALDIALOGEVENT g_agent_latest_dialog_event;
static PALPALDIALOGEVENT g_agent_dialog_events[PALPAL_AGENT_DIALOG_EVENT_CAPACITY];
static int g_agent_dialog_event_next = 0;
static int g_agent_dialog_event_count = 0;

typedef struct tagPALPALAGENTCHOICE
{
   BOOL active;
   BOOL menu_started;
   DWORD generation;
   WORD event_object_id;
   WORD script_entry;
   int selected_index;
   BOOL command_pending;
   int requested_index;
} PALPALAGENTCHOICE;

static PALPALAGENTCHOICE g_agent_choice;

#define PALPAL_DIALOG_ACTIVE_GRACE_MS 1000

static VOID
PALPAL_AgentInit(
   VOID
)
{
   const char *enabled;
   const char *headless;
   const char *runtime_dir;
   const char *socket_path;
   const char *game_instance_id;
   int i;

   if (g_agent_initialized)
   {
      return;
   }

   g_agent_initialized = 1;
   enabled = getenv("PALPAL_AGENT");
   g_agent_enabled = (enabled && strcmp(enabled, "0") != 0);

   headless = getenv("PALPAL_HEADLESS");
   g_agent_headless = (headless && strcmp(headless, "0") != 0);

   runtime_dir = getenv("PALPAL_RUNTIME_DIR");
   if (runtime_dir && runtime_dir[0])
   {
      strncpy(g_agent_runtime_dir, runtime_dir, sizeof(g_agent_runtime_dir) - 1);
      g_agent_runtime_dir[sizeof(g_agent_runtime_dir) - 1] = '\0';
   }

   socket_path = getenv("PALPAL_AGENT_SOCKET");
   if (socket_path && socket_path[0])
   {
      strncpy(g_agent_socket_path, socket_path, sizeof(g_agent_socket_path) - 1);
      g_agent_socket_path[sizeof(g_agent_socket_path) - 1] = '\0';
   }
   else
   {
      snprintf(g_agent_socket_path, sizeof(g_agent_socket_path), "%s/live.sock", g_agent_runtime_dir);
   }

   game_instance_id = getenv("PALPAL_GAME_INSTANCE_ID");
   if (game_instance_id && game_instance_id[0])
   {
      strncpy(g_agent_game_instance_id, game_instance_id, sizeof(g_agent_game_instance_id) - 1);
      g_agent_game_instance_id[sizeof(g_agent_game_instance_id) - 1] = '\0';
      for (i = 0; g_agent_game_instance_id[i] != '\0'; i++)
      {
         char c = g_agent_game_instance_id[i];
         if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
               (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.'))
         {
            g_agent_game_instance_id[i] = '_';
         }
      }
   }
   else
   {
      snprintf(g_agent_game_instance_id, sizeof(g_agent_game_instance_id),
         "%08lx-%08x", (unsigned long)time(NULL), (unsigned int)SDL_GetTicks());
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

BOOL
PALPAL_AgentStartupWaitMode(
   VOID
)
{
   const char *mode;

   if (!PALPAL_AgentEnabled())
   {
      return FALSE;
   }

   mode = getenv("PALPAL_START_MODE");
   if (mode == NULL || mode[0] == '\0')
   {
      return TRUE;
   }
   return strcmp(mode, "wait") == 0 ? TRUE : FALSE;
}

VOID
PALPAL_AgentBeginStartupWait(
   VOID
)
{
   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   g_agent_startup_slot = PALPAL_AgentSaveSlot();
   g_agent_startup_ready = FALSE;
   g_agent_startup_waiting = TRUE;
}

BOOL
PALPAL_AgentStartupReady(
   VOID
)
{
   return g_agent_startup_ready ? TRUE : FALSE;
}

INT
PALPAL_AgentStartupSlot(
   VOID
)
{
   return g_agent_startup_slot;
}

BOOL
PALPAL_AgentHeadless(
   VOID
)
{
   PALPAL_AgentInit();
   return g_agent_headless ? TRUE : FALSE;
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

static WORD
PALPAL_AgentCurrentMapId(
   VOID
)
{
   int scene_index;

   if (gpGlobals == NULL)
   {
      return 0;
   }

   scene_index = gpGlobals->wNumScene > 0 ? gpGlobals->wNumScene - 1 : 0;
   if (scene_index >= 0 && scene_index < MAX_SCENES)
   {
      return gpGlobals->g.rgScene[scene_index].wMapNum;
   }
   return 0;
}

static VOID
PALPAL_AgentClearDialogSpeaker(
   VOID
)
{
   g_agent_dialog_speaker[0] = 0;
   g_agent_dialog_speaker_bound = FALSE;
   g_agent_dialog_speaker_scene_id = 0;
   g_agent_dialog_speaker_map_id = 0;
   g_agent_dialog_speaker_event_object_id = 0;
}

VOID
PALPAL_AgentBeginTriggerScript(
   WORD       wEventObjectID,
   WORD       wTriggerScript
)
{
   if (!PALPAL_AgentEnabled() || gpGlobals == NULL || !gpGlobals->fInMainGame || wEventObjectID == 0xFFFF)
   {
      return;
   }

   PALPAL_AgentClearDialogSpeaker();
   g_agent_trigger_observation.valid = TRUE;
   g_agent_trigger_observation.completed = FALSE;
   g_agent_trigger_observation.generation++;
   g_agent_trigger_observation.event_object_id = wEventObjectID;
   g_agent_trigger_observation.trigger_script = wTriggerScript;
   g_agent_trigger_observation.source_scene_id = gpGlobals->wNumScene;
   g_agent_trigger_observation.source_map_id = PALPAL_AgentCurrentMapId();
   g_agent_trigger_observation.source_x = PAL_X(gpGlobals->viewport) + PAL_X(gpGlobals->partyoffset);
   g_agent_trigger_observation.source_y = PAL_Y(gpGlobals->viewport) + PAL_Y(gpGlobals->partyoffset);
   g_agent_trigger_observation.destination_scene_id = 0;
   g_agent_trigger_observation.destination_map_id = 0;
   g_agent_trigger_observation.destination_x = 0;
   g_agent_trigger_observation.destination_y = 0;
   g_agent_trigger_observation.destination_frame = 0;
}

VOID
PALPAL_AgentEndTriggerScript(
   WORD       wEventObjectID
)
{
   /* The bridge reports execution, not a guessed room. */
   if (g_agent_trigger_observation.valid && g_agent_trigger_observation.event_object_id == wEventObjectID)
   {
      g_agent_trigger_observation.destination_scene_id = gpGlobals->wNumScene;
      g_agent_trigger_observation.destination_map_id = PALPAL_AgentCurrentMapId();
      g_agent_trigger_observation.destination_x = PAL_X(gpGlobals->viewport) + PAL_X(gpGlobals->partyoffset);
      g_agent_trigger_observation.destination_y = PAL_Y(gpGlobals->viewport) + PAL_Y(gpGlobals->partyoffset);
      g_agent_trigger_observation.destination_frame = gpGlobals->dwFrameNum;
      g_agent_trigger_observation.completed = TRUE;
   }
}

static VOID
PALPAL_AgentWriteJsonCodepoint(
   FILE *fp,
   unsigned int codepoint
)
{
   if (codepoint <= 0x7F)
   {
      fputc((int)codepoint, fp);
   }
   else if (codepoint <= 0x7FF)
   {
      fputc(0xC0 | (codepoint >> 6), fp);
      fputc(0x80 | (codepoint & 0x3F), fp);
   }
   else if (codepoint <= 0xFFFF)
   {
      fputc(0xE0 | (codepoint >> 12), fp);
      fputc(0x80 | ((codepoint >> 6) & 0x3F), fp);
      fputc(0x80 | (codepoint & 0x3F), fp);
   }
   else
   {
      fputc(0xF0 | (codepoint >> 18), fp);
      fputc(0x80 | ((codepoint >> 12) & 0x3F), fp);
      fputc(0x80 | ((codepoint >> 6) & 0x3F), fp);
      fputc(0x80 | (codepoint & 0x3F), fp);
   }
}

static VOID
PALPAL_AgentWriteJsonWideString(
   FILE *fp,
   LPCWSTR text
)
{
   const WCHAR *p = text ? text : L"";

   fputc('"', fp);
   while (*p)
   {
      unsigned int codepoint = (unsigned int)*p++;

      if (codepoint == '"')
      {
         fputs("\\\"", fp);
      }
      else if (codepoint == '\\')
      {
         fputs("\\\\", fp);
      }
      else if (codepoint == '\b')
      {
         fputs("\\b", fp);
      }
      else if (codepoint == '\f')
      {
         fputs("\\f", fp);
      }
      else if (codepoint == '\n')
      {
         fputs("\\n", fp);
      }
      else if (codepoint == '\r')
      {
         fputs("\\r", fp);
      }
      else if (codepoint == '\t')
      {
         fputs("\\t", fp);
      }
      else if (codepoint < 0x20)
      {
         fprintf(fp, "\\u%04x", codepoint);
      }
      else
      {
         if (sizeof(WCHAR) == 2 && codepoint >= 0xD800 && codepoint <= 0xDBFF && *p >= 0xDC00 && *p <= 0xDFFF)
         {
            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + ((unsigned int)*p++ - 0xDC00);
         }
         if (codepoint > 0x10FFFF || (codepoint >= 0xD800 && codepoint <= 0xDFFF))
         {
            codepoint = '?';
         }
         PALPAL_AgentWriteJsonCodepoint(fp, codepoint);
      }
   }
   fputc('"', fp);
}

static VOID
PALPAL_AgentCopyWideString(
   WCHAR *dst,
   size_t dst_len,
   LPCWSTR src
)
{
   size_t i;

   if (dst_len == 0)
   {
      return;
   }

   if (src == NULL)
   {
      dst[0] = 0;
      return;
   }

   for (i = 0; i + 1 < dst_len && src[i]; i++)
   {
      dst[i] = src[i];
   }
   dst[i] = 0;
}

static const char *
PALPAL_AgentEquipmentSlotKey(
   int slot
)
{
   if (slot >= 0 && slot < MAX_PLAYER_EQUIPMENTS)
   {
      return g_agent_equipment_slot_keys[slot];
   }
   return "unknown";
}

static const char *
PALPAL_AgentStatusKey(
   WORD status
)
{
   switch (status)
   {
   case kStatusConfused: return "confused";
#ifdef PAL_CLASSIC
   case kStatusParalyzed: return "paralyzed";
#else
   case kStatusSlow: return "slow";
#endif
   case kStatusSleep: return "sleep";
   case kStatusSilence: return "silence";
   case kStatusPuppet: return "puppet";
   case kStatusBravery: return "bravery";
   case kStatusProtect: return "protect";
   case kStatusHaste: return "haste";
   case kStatusDualAttack: return "dual_attack";
   default: return "unknown";
   }
}

static const char *
PALPAL_AgentItemEffectKind(
   PALPALITEMEFFECTKIND kind
)
{
   switch (kind)
   {
   case kPalpalItemEffectRestoreHP: return "restore_hp";
   case kPalpalItemEffectRestoreMP: return "restore_mp";
   case kPalpalItemEffectRestoreHPMP: return "restore_hp_mp";
   case kPalpalItemEffectRevive: return "revive";
   case kPalpalItemEffectApplyPoison: return "apply_poison";
   case kPalpalItemEffectCurePoison: return "cure_poison";
   case kPalpalItemEffectCurePoisonLevel: return "cure_poison_level";
   case kPalpalItemEffectApplyStatus: return "apply_status";
   case kPalpalItemEffectRemoveStatus: return "remove_status";
   case kPalpalItemEffectEnemyDamage: return "enemy_damage";
   case kPalpalItemEffectDrainHP: return "drain_hp";
   default: return "unknown";
   }
}

static VOID
PALPAL_AgentRememberItemEffect(
   PALPALITEMEFFECTPREVIEW *preview,
   PALPALITEMEFFECTKIND kind,
   WORD script_entry,
   LPSCRIPTENTRY entry,
   BOOL conditional
)
{
   PALPALITEMEFFECT *effect;

   if (preview->effect_count >= PALPAL_AGENT_ITEM_EFFECT_LIMIT)
   {
      preview->complete = FALSE;
      return;
   }
   effect = &preview->effects[preview->effect_count++];
   effect->kind = kind;
   effect->script_entry = script_entry;
   effect->operand0 = entry->rgwOperand[0];
   effect->operand1 = entry->rgwOperand[1];
   effect->operand2 = entry->rgwOperand[2];
   effect->conditional = conditional;
}

static VOID
PALPAL_AgentRememberItemRequirement(
   PALPALITEMEFFECTPREVIEW *preview,
   int kind,
   WORD script_entry,
   LPSCRIPTENTRY entry,
   BOOL conditional
)
{
   PALPALITEMREQUIREMENT *requirement;
   int i;

   for (i = 0; i < preview->requirement_count; i++)
   {
      requirement = &preview->requirements[i];
      if (requirement->kind == kind &&
         requirement->script_entry == script_entry &&
         requirement->event_object_id == entry->rgwOperand[0])
      {
         return;
      }
   }

   if (preview->requirement_count >= PALPAL_AGENT_ITEM_REQUIREMENT_LIMIT)
   {
      preview->requirements_complete = FALSE;
      return;
   }
   requirement = &preview->requirements[preview->requirement_count++];
   requirement->kind = kind;
   requirement->script_entry = script_entry;
   requirement->event_object_id = entry->rgwOperand[0];
   requirement->distance_level = entry->rgwOperand[1];
   requirement->failure_script_entry = entry->rgwOperand[2];
   requirement->conditional = conditional;
}

static VOID
PALPAL_AgentRememberUnknownItemEffect(
   PALPALITEMEFFECTPREVIEW *preview,
   WORD script_entry,
   LPSCRIPTENTRY entry
)
{
   PALPALEQUIPMENTSPECIAL *effect;

   preview->complete = FALSE;
   if (preview->unknown_count >= PALPAL_AGENT_ITEM_UNKNOWN_LIMIT)
   {
      return;
   }
   effect = &preview->unknown[preview->unknown_count];
   preview->unknown_entries[preview->unknown_count] = script_entry;
   effect->operation = entry->wOperation;
   effect->operand0 = entry->rgwOperand[0];
   effect->operand1 = entry->rgwOperand[1];
   effect->operand2 = entry->rgwOperand[2];
   preview->unknown_count++;
}

static PALPALITEMEFFECTPREVIEW
PALPAL_AgentPreviewItemScript(
   WORD script_entry
)
{
   PALPALITEMEFFECTPREVIEW preview;
   struct
   {
      WORD entry;
      BOOL conditional;
   } pending[PALPAL_AGENT_ITEM_SCRIPT_LIMIT];
   WORD visited[PALPAL_AGENT_ITEM_SCRIPT_LIMIT];
   int pending_count = 0;
   int visited_count = 0;
   int steps = 0;

   memset(&preview, 0, sizeof(preview));
   preview.complete = TRUE;
   preview.requirements_complete = TRUE;
   preview.script_entry = script_entry;
   if (script_entry == 0)
   {
      return preview;
   }
   pending[pending_count].entry = script_entry;
   pending[pending_count].conditional = FALSE;
   pending_count++;

   while (pending_count > 0 && steps < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
   {
      WORD current;
      BOOL conditional;

      pending_count--;
      current = pending[pending_count].entry;
      conditional = pending[pending_count].conditional;
      while (current != 0 && steps < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
      {
         LPSCRIPTENTRY entry;
         int i;
         BOOL seen = FALSE;

         if (current >= gpGlobals->g.nScriptEntry)
         {
            preview.complete = FALSE;
            preview.requirements_complete = FALSE;
            break;
         }
         for (i = 0; i < visited_count; i++)
         {
            if (visited[i] == current)
            {
               seen = TRUE;
               break;
            }
         }
         if (seen)
         {
            break;
         }
         visited[visited_count++] = current;
         steps++;
         entry = &gpGlobals->g.lprgScriptEntry[current];

         switch (entry->wOperation)
         {
         case 0x0000:
         case 0x0001:
            current = 0;
            break;

         case 0x0003:
            PALPAL_AgentRememberUnknownItemEffect(&preview, current, entry);
            if (entry->rgwOperand[1] == 0)
            {
               current = entry->rgwOperand[0];
            }
            else
            {
               if (pending_count < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
               {
                  pending[pending_count].entry = entry->rgwOperand[0];
                  pending[pending_count].conditional = TRUE;
                  pending_count++;
               }
               else
               {
                  preview.requirements_complete = FALSE;
               }
               current++;
               conditional = TRUE;
            }
            break;

         case 0x0004:
            if (pending_count < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
            {
               pending[pending_count].entry = entry->rgwOperand[0];
               pending[pending_count].conditional = conditional;
               pending_count++;
            }
            else
            {
               preview.complete = FALSE;
               preview.requirements_complete = FALSE;
            }
            current++;
            break;

         case 0x0006:
         case 0x000A:
            PALPAL_AgentRememberUnknownItemEffect(&preview, current, entry);
            if (pending_count < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
            {
               pending[pending_count].entry =
                  entry->wOperation == 0x0006 ? entry->rgwOperand[1] : entry->rgwOperand[0];
               pending[pending_count].conditional = TRUE;
               pending_count++;
            }
            else
            {
               preview.requirements_complete = FALSE;
            }
            current++;
            conditional = TRUE;
            break;

         case 0x001B:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectRestoreHP, current, entry, conditional);
            current++;
            break;
         case 0x001C:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectRestoreMP, current, entry, conditional);
            current++;
            break;
         case 0x001D:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectRestoreHPMP, current, entry, conditional);
            current++;
            break;
         case 0x0021:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectEnemyDamage, current, entry, conditional);
            current++;
            break;
         case 0x0022:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectRevive, current, entry, conditional);
            current++;
            break;
         case 0x0029:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectApplyPoison, current, entry, conditional);
            current++;
            break;
         case 0x002B:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectCurePoison, current, entry, conditional);
            current++;
            break;
         case 0x002C:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectCurePoisonLevel, current, entry, conditional);
            current++;
            break;
         case 0x002D:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectApplyStatus, current, entry, conditional);
            current++;
            break;
         case 0x002F:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectRemoveStatus, current, entry, conditional);
            current++;
            break;
         case 0x0039:
            PALPAL_AgentRememberItemEffect(
               &preview, kPalpalItemEffectDrainHP, current, entry, conditional);
            current++;
            break;

         case 0x0081:
            PALPAL_AgentRememberItemRequirement(
               &preview, kPalpalItemRequirementFaceEventObject,
               current, entry, conditional);
            if (entry->rgwOperand[2] != 0)
            {
               if (pending_count < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
               {
                  pending[pending_count].entry = entry->rgwOperand[2];
                  pending[pending_count].conditional = TRUE;
                  pending_count++;
               }
               else
               {
                  preview.requirements_complete = FALSE;
               }
            }
            current++;
            conditional = TRUE;
            break;

         case 0x0084:
            PALPAL_AgentRememberItemRequirement(
               &preview, kPalpalItemRequirementPlaceEventObject,
               current, entry, conditional);
            if (entry->rgwOperand[2] != 0)
            {
               if (pending_count < PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
               {
                  pending[pending_count].entry = entry->rgwOperand[2];
                  pending[pending_count].conditional = TRUE;
                  pending_count++;
               }
               else
               {
                  preview.requirements_complete = FALSE;
               }
            }
            current++;
            conditional = TRUE;
            break;

         default:
            PALPAL_AgentRememberUnknownItemEffect(&preview, current, entry);
            current++;
            break;
         }
      }
   }
   if (pending_count > 0 || steps >= PALPAL_AGENT_ITEM_SCRIPT_LIMIT)
   {
      preview.complete = FALSE;
      preview.requirements_complete = FALSE;
   }
   return preview;
}

static VOID
PALPAL_AgentWriteItemEffectPreview(
   FILE *fp,
   PALPALITEMEFFECTPREVIEW *preview
)
{
   int i;

   fprintf(fp,
      "{\"script_entry\":%u,\"preview_complete\":%s,"
      "\"requirements_complete\":%s,\"effects\":[",
      preview->script_entry, preview->complete ? "true" : "false",
      preview->requirements_complete ? "true" : "false");
   for (i = 0; i < preview->effect_count; i++)
   {
      PALPALITEMEFFECT *effect = &preview->effects[i];
      if (i > 0) fprintf(fp, ",");
      fprintf(fp,
         "{\"kind\":\"%s\",\"script_entry\":%u,\"conditional\":%s",
         PALPAL_AgentItemEffectKind(effect->kind), effect->script_entry,
         effect->conditional ? "true" : "false");
      switch (effect->kind)
      {
      case kPalpalItemEffectRestoreHP:
      case kPalpalItemEffectRestoreMP:
      case kPalpalItemEffectRestoreHPMP:
         fprintf(fp, ",\"amount\":%d,\"apply_to_all\":%s",
            (SHORT)effect->operand1, effect->operand0 ? "true" : "false");
         break;
      case kPalpalItemEffectRevive:
         fprintf(fp, ",\"max_hp_percent\":%u,\"apply_to_all\":%s",
            effect->operand1 * 10, effect->operand0 ? "true" : "false");
         break;
      case kPalpalItemEffectApplyPoison:
      case kPalpalItemEffectCurePoison:
         fprintf(fp, ",\"poison_id\":%u,\"poison_name\":",
            effect->operand1);
         PALPAL_AgentWriteJsonWideString(
            fp, effect->operand1 < MAX_OBJECTS ? PAL_GetWord(effect->operand1) : L"");
         fprintf(fp, ",\"apply_to_all\":%s", effect->operand0 ? "true" : "false");
         break;
      case kPalpalItemEffectCurePoisonLevel:
         fprintf(fp, ",\"max_poison_level\":%u,\"apply_to_all\":%s",
            effect->operand1, effect->operand0 ? "true" : "false");
         break;
      case kPalpalItemEffectApplyStatus:
         fprintf(fp,
            ",\"status_id\":%u,\"status\":\"%s\",\"duration\":%u",
            effect->operand0, PALPAL_AgentStatusKey(effect->operand0), effect->operand1);
         break;
      case kPalpalItemEffectRemoveStatus:
         fprintf(fp, ",\"status_id\":%u,\"status\":\"%s\"",
            effect->operand0, PALPAL_AgentStatusKey(effect->operand0));
         break;
      case kPalpalItemEffectEnemyDamage:
         fprintf(fp, ",\"amount\":%u,\"apply_to_all\":%s",
            effect->operand1, effect->operand0 ? "true" : "false");
         break;
      case kPalpalItemEffectDrainHP:
         fprintf(fp, ",\"amount\":%u", effect->operand0);
         break;
      }
      fprintf(fp, "}");
   }
   fprintf(fp, "],\"requirements\":[");
   for (i = 0; i < preview->requirement_count; i++)
   {
      PALPALITEMREQUIREMENT *requirement = &preview->requirements[i];
      if (i > 0) fprintf(fp, ",");
      if (requirement->kind == kPalpalItemRequirementFaceEventObject)
      {
         fprintf(fp,
            "{\"kind\":\"face_event_object\",\"script_entry\":%u,"
            "\"conditional\":%s,\"event_object_id\":%u,"
            "\"distance_level\":%u,\"distance_limit\":%u,"
            "\"failure_script_entry\":%u}",
            requirement->script_entry,
            requirement->conditional ? "true" : "false",
            requirement->event_object_id,
            requirement->distance_level,
            (unsigned int)(requirement->distance_level * 32 + 16),
            requirement->failure_script_entry);
      }
      else
      {
         fprintf(fp,
            "{\"kind\":\"place_event_object\",\"script_entry\":%u,"
            "\"conditional\":%s,\"event_object_id\":%u,"
            "\"failure_script_entry\":%u}",
            requirement->script_entry,
            requirement->conditional ? "true" : "false",
            requirement->event_object_id,
            requirement->failure_script_entry);
      }
   }
   fprintf(fp, "],\"unparsed_effects\":[");
   for (i = 0; i < preview->unknown_count; i++)
   {
      PALPALEQUIPMENTSPECIAL *effect = &preview->unknown[i];
      if (i > 0) fprintf(fp, ",");
      fprintf(fp,
         "{\"script_entry\":%u,\"operation\":%u,\"operands\":[%u,%u,%u]}",
         preview->unknown_entries[i], effect->operation,
         effect->operand0, effect->operand1, effect->operand2);
   }
   fprintf(fp, "]}");
}

static VOID
PALPAL_AgentRememberEquipmentEffect(
   PALPALEQUIPMENTSPECIAL *effects,
   int *count,
   int capacity,
   LPSCRIPTENTRY entry
)
{
   PALPALEQUIPMENTSPECIAL *effect;

   if (*count >= capacity)
   {
      return;
   }
   effect = &effects[*count];
   effect->operation = entry->wOperation;
   effect->operand0 = entry->rgwOperand[0];
   effect->operand1 = entry->rgwOperand[1];
   effect->operand2 = entry->rgwOperand[2];
   (*count)++;
}

static PALPALEQUIPMENTPREVIEW
PALPAL_AgentPreviewEquipment(
   WORD item_id
)
{
   PALPALEQUIPMENTPREVIEW preview;
   WORD script_entry;
   int steps;

   memset(&preview, 0, sizeof(preview));
   preview.complete = TRUE;
   preview.slot_id = -1;
   if (item_id == 0 || item_id >= MAX_OBJECTS ||
      !(gpGlobals->g.rgObject[item_id].item.wFlags & kItemFlagEquipable))
   {
      preview.complete = FALSE;
      return preview;
   }

   script_entry = gpGlobals->g.rgObject[item_id].item.wScriptOnEquip;
   for (steps = 0; script_entry != 0 && steps < PALPAL_AGENT_EQUIP_SCRIPT_LIMIT; steps++, script_entry++)
   {
      LPSCRIPTENTRY entry;
      int field;

      if (script_entry >= gpGlobals->g.nScriptEntry)
      {
         preview.complete = FALSE;
         break;
      }
      entry = &gpGlobals->g.lprgScriptEntry[script_entry];
      if (entry->wOperation == 0x0000)
      {
         break;
      }
      switch (entry->wOperation)
      {
      case 0x0018:
         preview.slot_id = (int)entry->rgwOperand[0] - 0x0B;
         if (preview.slot_id < 0 || preview.slot_id >= MAX_PLAYER_EQUIPMENTS ||
            entry->rgwOperand[1] != item_id)
         {
            preview.complete = FALSE;
         }
         break;

      case 0x0017:
         field = entry->rgwOperand[1];
         if ((int)entry->rgwOperand[0] - 0x0B != preview.slot_id && preview.slot_id >= 0)
         {
            preview.complete = FALSE;
         }
         switch (field)
         {
         case 17: preview.attack_strength = (SHORT)entry->rgwOperand[2]; break;
         case 18: preview.magic_strength = (SHORT)entry->rgwOperand[2]; break;
         case 19: preview.defense = (SHORT)entry->rgwOperand[2]; break;
         case 20: preview.dexterity = (SHORT)entry->rgwOperand[2]; break;
         case 21: preview.flee_rate = (SHORT)entry->rgwOperand[2]; break;
         case 22: preview.poison_resistance = (SHORT)entry->rgwOperand[2]; break;
         default:
            if (field >= 23 && field < 23 + NUM_MAGIC_ELEMENTAL)
            {
               preview.elemental_resistance[field - 23] = (SHORT)entry->rgwOperand[2];
            }
            else
            {
               preview.complete = FALSE;
               PALPAL_AgentRememberEquipmentEffect(
                  preview.unknown, &preview.unknown_count,
                  PALPAL_AGENT_EQUIP_UNKNOWN_LIMIT, entry);
            }
            break;
         }
         break;

      case 0x001A:
         switch (entry->rgwOperand[0])
         {
         case 1:
            preview.has_battle_sprite = TRUE;
            preview.battle_sprite = entry->rgwOperand[1];
            break;
         case 4:
            preview.has_attack_all = TRUE;
            preview.attack_all = entry->rgwOperand[1];
            break;
         case 65:
            preview.has_cooperative_magic = TRUE;
            preview.cooperative_magic = entry->rgwOperand[1];
            break;
         default:
            preview.complete = FALSE;
            PALPAL_AgentRememberEquipmentEffect(
               preview.unknown, &preview.unknown_count,
               PALPAL_AGENT_EQUIP_UNKNOWN_LIMIT, entry);
            break;
         }
         break;

      case 0x0029:
      case 0x002D:
         PALPAL_AgentRememberEquipmentEffect(
            preview.specials, &preview.special_count,
            PALPAL_AGENT_EQUIP_SPECIAL_LIMIT, entry);
         break;

      default:
         preview.complete = FALSE;
         PALPAL_AgentRememberEquipmentEffect(
            preview.unknown, &preview.unknown_count,
            PALPAL_AGENT_EQUIP_UNKNOWN_LIMIT, entry);
         break;
      }
   }
   if (script_entry != 0 &&
      (steps >= PALPAL_AGENT_EQUIP_SCRIPT_LIMIT || script_entry >= gpGlobals->g.nScriptEntry))
   {
      preview.complete = FALSE;
   }
   if (preview.slot_id < 0)
   {
      preview.complete = FALSE;
   }
   return preview;
}

static VOID
PALPAL_AgentWriteEquipmentPreview(
   FILE *fp,
   WORD item_id
)
{
   PALPALEQUIPMENTPREVIEW preview;
   int i;
   int first;

   preview = PALPAL_AgentPreviewEquipment(item_id);
   fprintf(fp,
      ",\"equipment_effect\":{"
      "\"preview_complete\":%s,"
      "\"slot_id\":%d,"
      "\"slot_key\":\"%s\","
      "\"stat_modifiers\":{"
      "\"attack_strength\":%d,"
      "\"magic_strength\":%d,"
      "\"defense\":%d,"
      "\"dexterity\":%d,"
      "\"flee_rate\":%d,"
      "\"poison_resistance\":%d},"
      "\"elemental_resistance\":[",
      preview.complete ? "true" : "false",
      preview.slot_id,
      PALPAL_AgentEquipmentSlotKey(preview.slot_id),
      preview.attack_strength,
      preview.magic_strength,
      preview.defense,
      preview.dexterity,
      preview.flee_rate,
      preview.poison_resistance);
   for (i = 0; i < NUM_MAGIC_ELEMENTAL; i++)
   {
      if (i > 0) fprintf(fp, ",");
      fprintf(fp, "%d", preview.elemental_resistance[i]);
   }
   fprintf(fp, "],\"traits\":[");
   first = 1;
   if (preview.has_battle_sprite)
   {
      fprintf(fp, "{\"kind\":\"battle_sprite\",\"value\":%u}", preview.battle_sprite);
      first = 0;
   }
   if (preview.has_attack_all)
   {
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"kind\":\"attack_all\",\"value\":%s}",
         preview.attack_all ? "true" : "false");
      first = 0;
   }
   if (preview.has_cooperative_magic)
   {
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"kind\":\"cooperative_magic\",\"value\":%u}",
         preview.cooperative_magic);
      first = 0;
   }
   for (i = 0; i < preview.special_count; i++)
   {
      PALPALEQUIPMENTSPECIAL *effect = &preview.specials[i];
      if (!first) fprintf(fp, ",");
      if (effect->operation == 0x002D)
      {
         fprintf(fp,
            "{\"kind\":\"status\",\"status_id\":%u,\"status\":\"%s\",\"duration\":%u}",
            effect->operand0,
            PALPAL_AgentStatusKey(effect->operand0),
            effect->operand1);
      }
      else
      {
         fprintf(fp,
            "{\"kind\":\"persistent_poison\",\"apply_to_all\":%s,\"poison_id\":%u,\"name\":",
            effect->operand0 ? "true" : "false",
            effect->operand1);
         PALPAL_AgentWriteJsonWideString(
            fp,
            effect->operand1 < MAX_OBJECTS ? PAL_GetWord(effect->operand1) : L"");
         fprintf(fp, "}");
      }
      first = 0;
   }
   fprintf(fp, "],\"unparsed_effects\":[");
   for (i = 0; i < preview.unknown_count; i++)
   {
      PALPALEQUIPMENTSPECIAL *effect = &preview.unknown[i];
      if (i > 0) fprintf(fp, ",");
      fprintf(fp,
         "{\"operation\":%u,\"operands\":[%u,%u,%u]}",
         effect->operation,
         effect->operand0,
         effect->operand1,
         effect->operand2);
   }
   fprintf(fp, "]}");
}

static VOID
PALPAL_AgentWriteItemFlags(
   FILE *fp,
   WORD flags
)
{
   fprintf(fp,
      "\"flags\":%u,"
      "\"usable\":%s,"
      "\"equipable\":%s,"
      "\"throwable\":%s,"
      "\"consuming\":%s,"
      "\"apply_to_all\":%s,"
      "\"sellable\":%s",
      (unsigned int)flags,
      (flags & kItemFlagUsable) ? "true" : "false",
      (flags & kItemFlagEquipable) ? "true" : "false",
      (flags & kItemFlagThrowable) ? "true" : "false",
      (flags & kItemFlagConsuming) ? "true" : "false",
      (flags & kItemFlagApplyToAll) ? "true" : "false",
      (flags & kItemFlagSellable) ? "true" : "false");
}

static int
PALPAL_AgentEquippedCount(
   WORD item_id
)
{
   int i, j, count = 0;

   if (item_id == 0 || gpGlobals == NULL)
   {
      return 0;
   }

   for (i = 0; i <= (int)gpGlobals->wMaxPartyMemberIndex; i++)
   {
      WORD role = gpGlobals->rgParty[i].wPlayerRole;
      for (j = 0; j < MAX_PLAYER_EQUIPMENTS; j++)
      {
         if (gpGlobals->g.PlayerRoles.rgwEquipment[j][role] == item_id)
         {
            count++;
         }
      }
   }
   return count;
}

static VOID
PALPAL_AgentWriteItemMetadata(
   FILE *fp,
   WORD item_id
)
{
   OBJECT item;
   PALPALITEMPREVIEWCACHE *cache = NULL;

   memset(&item, 0, sizeof(item));
   if (item_id != 0 && item_id < MAX_OBJECTS)
   {
      item = gpGlobals->g.rgObject[item_id];
   }

   fprintf(fp, "\"item_id\":%u,\"name\":", (unsigned int)item_id);
   PALPAL_AgentWriteJsonWideString(fp, (item_id != 0 && item_id < MAX_OBJECTS) ? PAL_GetWord(item_id) : L"");
   fprintf(fp,
      ",\"price\":%u,"
      "\"bitmap_id\":%u,"
      "\"script_on_use\":%u,"
      "\"script_on_equip\":%u,"
      "\"script_on_throw\":%u,",
      (unsigned int)item.item.wPrice,
      (unsigned int)item.item.wBitmap,
      (unsigned int)item.item.wScriptOnUse,
      (unsigned int)item.item.wScriptOnEquip,
      (unsigned int)item.item.wScriptOnThrow);
   PALPAL_AgentWriteItemFlags(fp, item.item.wFlags);
   if (item_id != 0 && item_id < MAX_OBJECTS)
   {
      LPCWSTR description = PALPAL_AgentItemDescription(item_id);
      if (description != NULL && description[0] != L'\0')
      {
         fprintf(fp, ",\"description\":");
         PALPAL_AgentWriteJsonWideString(fp, description);
      }
   }
   if (item_id != 0 && item_id < MAX_OBJECTS &&
      (item.item.wFlags & (kItemFlagUsable | kItemFlagThrowable)))
   {
      cache = &g_agent_item_preview_cache[item_id];
      if (!cache->valid ||
         cache->use_script_entry != item.item.wScriptOnUse ||
         cache->throw_script_entry != item.item.wScriptOnThrow)
      {
         cache->valid = TRUE;
         cache->use_script_entry = item.item.wScriptOnUse;
         cache->throw_script_entry = item.item.wScriptOnThrow;
         cache->use_preview = PALPAL_AgentPreviewItemScript(item.item.wScriptOnUse);
         cache->throw_preview = PALPAL_AgentPreviewItemScript(item.item.wScriptOnThrow);
      }
      if (item.item.wFlags & kItemFlagUsable)
      {
         fprintf(fp, ",\"use_effect\":");
         PALPAL_AgentWriteItemEffectPreview(fp, &cache->use_preview);
      }
      if (item.item.wFlags & kItemFlagThrowable)
      {
         fprintf(fp, ",\"throw_effect\":");
         PALPAL_AgentWriteItemEffectPreview(fp, &cache->throw_preview);
      }
   }
   if (item.item.wFlags & kItemFlagEquipable)
   {
      int i;
      int first = 1;
      fprintf(fp, ",\"equipable_role_ids\":[");
      for (i = 0; i < MAX_PLAYER_ROLES; i++)
      {
         if (item.item.wFlags & (kItemFlagEquipableByPlayerRole_First << i))
         {
            if (!first) fprintf(fp, ",");
            fprintf(fp, "%d", i);
            first = 0;
         }
      }
      fprintf(fp, "]");
      PALPAL_AgentWriteEquipmentPreview(fp, item_id);
   }
}

static VOID
PALPAL_AgentWriteActionResult(
   BOOL ok,
   const char *status,
   WORD item_id,
   WORD role_id,
   BOOL target_all,
   BOOL consumed,
   BOOL script_success,
   const char *error
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp,
      "{\"time_ms\":%u,"
      "\"ok\":%s,"
      "\"status\":\"%s\","
      "\"action\":\"use_item\","
      "\"item_id\":%u,"
      "\"role_id\":%u,"
      "\"target\":\"%s\","
      "\"consumed\":%s,"
      "\"script_success\":%s,"
      "\"error\":",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      (unsigned int)item_id,
      (unsigned int)role_id,
      target_all ? "all" : "role",
      consumed ? "true" : "false",
      script_success ? "true" : "false");
   if (error && error[0])
   {
      fprintf(fp, "\"%s\"", error);
   }
   else
   {
      fputs("null", fp);
   }
   fputs("}\n", fp);
   fclose(fp);
   rename(tmp_path, result_path);
}

static VOID
PALPAL_AgentWriteEquipResult(
   BOOL ok,
   const char *status,
   WORD item_id,
   WORD role_id,
   int slot_id,
   WORD previous_item_id,
   const char *error
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp,
      "{\"time_ms\":%u,"
      "\"ok\":%s,"
      "\"status\":\"%s\","
      "\"action\":\"equip_item\","
      "\"item_id\":%u,"
      "\"role_id\":%u,"
      "\"slot_id\":%d,"
      "\"slot_key\":\"%s\","
      "\"previous_item_id\":%u,"
      "\"error\":",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      (unsigned int)item_id,
      (unsigned int)role_id,
      slot_id,
      PALPAL_AgentEquipmentSlotKey(slot_id),
      (unsigned int)previous_item_id);
   if (error && error[0])
   {
      fprintf(fp, "\"%s\"", error);
   }
   else
   {
      fputs("null", fp);
   }
   fputs("}\n", fp);
   fclose(fp);
   rename(tmp_path, result_path);
}

static VOID
PALPAL_AgentWriteShopResult(
   BOOL ok,
   const char *status,
   const char *action,
   WORD store_id,
   WORD item_id,
   WORD price,
   DWORD cash_before,
   DWORD cash_after,
   int owned_before,
   int owned_after,
   const char *error
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp,
      "{\"time_ms\":%u,"
      "\"ok\":%s,"
      "\"status\":\"%s\","
      "\"action\":\"%s\","
      "\"store_id\":%u,"
      "\"item_id\":%u,"
      "\"price\":%u,"
      "\"cash_before\":%u,"
      "\"cash_after\":%u,"
      "\"owned_before\":%d,"
      "\"owned_after\":%d,"
      "\"error\":",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      action ? action : "unknown",
      (unsigned int)store_id,
      (unsigned int)item_id,
      (unsigned int)price,
      (unsigned int)cash_before,
      (unsigned int)cash_after,
      owned_before,
      owned_after);
   if (error && error[0])
   {
      fprintf(fp, "\"%s\"", error);
   }
   else
   {
      fputs("null", fp);
   }
   fputs("}\n", fp);
   fclose(fp);
   rename(tmp_path, result_path);
}

static VOID
PALPAL_AgentWriteSlotResult(
   BOOL ok,
   const char *status,
   const char *action,
   int slot,
   const char *error
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp,
      "{\"time_ms\":%u,"
      "\"ok\":%s,"
      "\"status\":\"%s\","
      "\"action\":\"%s\","
      "\"slot\":%d,"
      "\"request_id\":\"%s\","
      "\"error\":",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      action ? action : "unknown",
      slot, g_agent_slot_request_id);
   if (error && error[0])
   {
      fprintf(fp, "\"%s\"", error);
   }
   else
   {
      fputs("null", fp);
   }
   fputs("}\n", fp);
   fclose(fp);
   rename(tmp_path, result_path);
}

static VOID
PALPAL_AgentWriteBattleResult(
   BOOL ok,
   const char *status,
   DWORD battle_generation,
   DWORD command_generation,
   WORD player_index,
   const char *action_type,
   WORD object_id,
   SHORT target_index
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }
   fprintf(fp,
      "{\"time_ms\":%u,\"ok\":%s,\"status\":\"%s\","
      "\"action\":\"battle_action\",\"battle_generation\":%u,"
      "\"command_generation\":%u,\"player_index\":%u,"
      "\"action_type\":\"%s\",\"object_id\":%u,\"target_index\":%d,"
      "\"error\":%s%s%s}\n",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      (unsigned int)battle_generation,
      (unsigned int)command_generation,
      (unsigned int)player_index,
      action_type ? action_type : "",
      (unsigned int)object_id,
      (int)target_index,
      ok ? "" : "\"",
      ok ? "null" : (status ? status : "unknown"),
      ok ? "" : "\"");
   fclose(fp);
   rename(tmp_path, result_path);
}

static VOID
PALPAL_AgentWriteChoiceResult(
   BOOL ok,
   const char *status,
   DWORD generation,
   int requested_index,
   int selected_index,
   const char *error
)
{
   char result_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   PALPAL_AgentPath(result_path, sizeof(result_path), "action_result.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "action_result.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp) return;
   fprintf(fp,
      "{\"time_ms\":%u,\"ok\":%s,\"status\":\"%s\","
      "\"action\":\"choice_select\",\"generation\":%u,"
      "\"requested_index\":%d,\"selected_index\":%d,"
      "\"semantic\":\"%s\",\"error\":",
      (unsigned int)SDL_GetTicks(), ok ? "true" : "false",
      status ? status : "unknown", (unsigned int)generation,
      requested_index, selected_index,
      selected_index == 1 ? "yes" : "no");
   if (error && error[0]) fprintf(fp, "\"%s\"", error);
   else fputs("null", fp);
   fputs("}\n", fp);
   fclose(fp);
   rename(tmp_path, result_path);
}

static BOOL
PALPAL_AgentSlotCommandAllowed(
   const char *action,
   int slot
)
{
   if (slot < 1 || slot > 255)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "invalid_slot", action, slot, "invalid_slot");
      return FALSE;
   }
   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "not_in_main_game", action, slot, "not_in_main_game");
      return FALSE;
   }
   if (gpGlobals->fInBattle)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "in_battle", action, slot, "in_battle");
      return FALSE;
   }
   if (gpGlobals->fEnteringScene &&
       !(g_agent_checkpoint_failed && strcmp(action, "load_slot") == 0))
   {
      PALPAL_AgentWriteSlotResult(FALSE, "entering_scene", action, slot, "entering_scene");
      return FALSE;
   }
   if (PAL_IsInDialog())
   {
      PALPAL_AgentWriteSlotResult(FALSE, "in_dialog", action, slot, "in_dialog");
      return FALSE;
   }
   if (g_agent_action_pending || g_agent_action_count > 0 || g_agent_waiting_for_move)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "action_pending", action, slot, "action_pending");
      return FALSE;
   }
   return TRUE;
}

static VOID
PALPAL_AgentCheckpointControl(VOID)
{
   char path[PAL_MAX_PATH], result[PAL_MAX_PATH], temporary[PAL_MAX_PATH];
   char command[16], request[33], argument[PAL_MAX_PATH];
   FILE *fp;
   const char *status = "failed";
   PALPAL_AgentPath(path, sizeof(path), "checkpoint_control.txt");
   fp = fopen(path, "r");
   if (!fp) return;
   argument[0] = '\0';
   if (fscanf(fp, "%15s %32s", command, request) != 2)
   { fclose(fp); remove(path); return; }
   if (fgets(argument, sizeof(argument), fp))
   {
      size_t n = strlen(argument);
      while (n && (argument[n-1] == '\n' || argument[n-1] == '\r')) argument[--n] = '\0';
   }
   fclose(fp);
   remove(path);
   if (strcmp(command, "validate") == 0)
      status = PAL_ValidateSaveFile(argument[0] == ' ' ? argument + 1 : argument) ? "validated" : "invalid_save";
   else if (strcmp(command, "pause") == 0)
   {
      if (g_agent_checkpoint_paused || g_agent_startup_waiting || (gpGlobals && !gpGlobals->fInBattle &&
          !gpGlobals->fEnteringScene && !PAL_IsInDialog() && !g_agent_action_pending))
      { g_agent_checkpoint_paused = TRUE; status = "paused"; }
      else status = "unsafe_pause";
   }
   else if (strcmp(command, "resume") == 0 && !g_agent_checkpoint_failed)
   { g_agent_checkpoint_paused = FALSE; status = "resumed"; }
   PALPAL_AgentPath(result, sizeof(result), "checkpoint_control_result.json");
   snprintf(temporary, sizeof(temporary), "%s.tmp", result);
   fp = fopen(temporary, "w");
   if (!fp) return;
   fprintf(fp, "{\"request_id\":\"%s\",\"status\":\"%s\",\"frame\":%u}\n", request, status, gpGlobals ? (unsigned int)gpGlobals->dwFrameNum : 0);
   fclose(fp);
   rename(temporary, result);
}

VOID
PALPAL_AgentCheckpointWait(VOID)
{
   if (!PALPAL_AgentEnabled()) return;
   PALPAL_AgentCheckpointControl();
   while (g_agent_checkpoint_paused && !g_agent_checkpoint_loading)
   {
      PALPAL_AgentWriteState();
      PALPAL_AgentPollAction();
      PALPAL_AgentCheckpointControl();
      SDL_Delay(10);
   }
}

VOID
PALPAL_AgentCheckpointLoadFailed(VOID)
{
   g_agent_checkpoint_failed = TRUE;
   g_agent_checkpoint_paused = TRUE;
   g_agent_checkpoint_loading = FALSE;
   strcpy(g_agent_slot_request_id, g_agent_loading_request_id);
   PALPAL_AgentWriteSlotResult(FALSE, "load_failed", g_agent_loading_action,
      g_agent_loading_slot, "invalid_save");
}

BOOL
PALPAL_AgentCheckpointLoading(VOID)
{ return g_agent_checkpoint_loading; }

VOID
PALPAL_AgentCheckpointLoaded(VOID)
{
   if (!g_agent_checkpoint_loading || g_agent_checkpoint_failed) return;
   g_agent_checkpoint_loading = FALSE;
   strcpy(g_agent_slot_request_id, g_agent_loading_request_id);
   PALPAL_AgentWriteSlotResult(TRUE,
      strcmp(g_agent_loading_action, "start_slot") == 0 ? "started" : "loaded",
      g_agent_loading_action, g_agent_loading_slot, NULL);
}

/* Events from the abandoned live branch must not be replayed after restore.
 * Keep sequence counters monotonic so future native evidence cannot collide. */
static VOID
PALPAL_AgentClearCheckpointObservations(VOID)
{
   g_agent_trigger_observation.valid = FALSE;
   g_agent_trigger_observation.completed = FALSE;
   g_agent_latest_dialog_event.valid = FALSE;
   g_agent_dialog_event_count = 0;
   g_agent_dialog_event_next = 0;
   g_agent_last_dialog_ticks = 0;
   g_agent_dialog_speaker_bound = FALSE;
   g_agent_dialog_speaker[0] = L'\0';
   g_agent_last_battle.valid = FALSE;
   g_agent_visible_outcome_count = 0;
   g_agent_visible_active.valid = FALSE;
   g_agent_choice.active = FALSE;
   g_agent_shop.active = FALSE;
}

static BOOL
PALPAL_AgentBeginSlotLoad(const char *action, int slot)
{
   char path[PAL_MAX_PATH];
   if (slot < 0 || slot > 255 || (strcmp(action, "load_slot") == 0 && slot == 0) ||
       (strcmp(action, "start_slot") == 0 && slot != 0))
   { PALPAL_AgentWriteSlotResult(FALSE, "invalid_slot", action, slot, "invalid_slot"); return FALSE; }
   if (slot)
   {
      UTIL_CombinePath(path, sizeof(path), 2, gConfig.pszSavePath, PAL_va(1, "%d.rpg", slot));
      if (!PAL_ValidateSaveFile(path))
      { PALPAL_AgentWriteSlotResult(FALSE, "invalid_save", action, slot, "invalid_save"); return FALSE; }
   }
   PALPAL_AgentClearCheckpointObservations();
   strcpy(g_agent_loading_request_id, g_agent_slot_request_id);
   strncpy(g_agent_loading_action, action, sizeof(g_agent_loading_action) - 1);
   g_agent_loading_slot = slot;
   g_agent_checkpoint_loading = TRUE;
   g_agent_checkpoint_paused = TRUE;
   g_agent_checkpoint_failed = FALSE;
   return TRUE;
}

static VOID
PALPAL_AgentDoStartupSlotAction(
   const char *action,
   int slot
)
{
   const char *status;

   if (slot < 0)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "invalid_slot", action, slot, "invalid_slot");
      return;
   }

   if (!PALPAL_AgentBeginSlotLoad(action, slot)) return;
   g_agent_startup_slot = slot;
   g_agent_startup_ready = TRUE;
   g_agent_startup_waiting = FALSE;
   status = (strcmp(action, "start_slot") == 0) ? "start_scheduled" : "load_scheduled";
   PALPAL_AgentWriteSlotResult(TRUE, status, action, slot, NULL);
}

static WORD
PALPAL_AgentSavedTimes(
   int slot
)
{
   char filename[32];
   FILE *fp;
   WORD saved_times = 0;

   snprintf(filename, sizeof(filename), "%d.rpg", slot);
   fp = UTIL_OpenFileAtPath(gConfig.pszSavePath, filename);
   if (!fp)
   {
      return 0;
   }
   if (fread(&saved_times, sizeof(WORD), 1, fp) == 1)
   {
      saved_times = SDL_SwapLE16(saved_times);
   }
   else
   {
      saved_times = 0;
   }
   fclose(fp);
   return saved_times;
}

static WORD
PALPAL_AgentNextSavedTimes(
   VOID
)
{
   WORD saved_times = 0;
   int i;

   for (i = 1; i <= 99; i++)
   {
      WORD current = PALPAL_AgentSavedTimes(i);
      if (current > saved_times)
      {
         saved_times = current;
      }
   }
   return saved_times + 1;
}

static VOID
PALPAL_AgentLoadStartupActionFile(
   VOID
)
{
   char path[PAL_MAX_PATH];
   FILE *fp;
   char line[128];
   int pending_slot = -1;
   char pending_action[32];

   PALPAL_AgentPath(path, sizeof(path), "action.txt");
   fp = fopen(path, "r");
   if (!fp)
   {
      return;
   }

   pending_action[0] = '\0';
   while (fgets(line, sizeof(line), fp))
   {
      char key[32], value[64];
      if (sscanf(line, "%31s %63s", key, value) != 2)
      {
         continue;
      }
      if (strcmp(key, "request_id") == 0)
      {
         strncpy(g_agent_slot_request_id, value, sizeof(g_agent_slot_request_id) - 1);
         g_agent_slot_request_id[32] = '\0';
         continue;
      }
      if (strcmp(key, "start_slot") == 0 || strcmp(key, "load_slot") == 0)
      {
         strncpy(pending_action, key, sizeof(pending_action) - 1);
         pending_action[sizeof(pending_action) - 1] = '\0';
         pending_slot = atoi(value);
         break;
      }
      if (strcmp(key, "save_slot") == 0)
      {
         PALPAL_AgentWriteSlotResult(FALSE, "not_in_main_game", key, atoi(value), "not_in_main_game");
      }
   }

   fclose(fp);
   remove(path);
   if (pending_action[0])
   {
      PALPAL_AgentDoStartupSlotAction(pending_action, pending_slot);
   }
}

static VOID
PALPAL_AgentDoSaveSlot(
   int slot
)
{
   if (!PALPAL_AgentSlotCommandAllowed("save_slot", slot))
   {
      return;
   }

   if (PAL_SaveGame(slot, PALPAL_AgentNextSavedTimes()))
      PALPAL_AgentWriteSlotResult(TRUE, "saved", "save_slot", slot, NULL);
   else
      PALPAL_AgentWriteSlotResult(FALSE, "save_failed", "save_slot", slot, "save_io_error");
}

static VOID
PALPAL_AgentDoLoadSlot(
   int slot
)
{
   if (!PALPAL_AgentSlotCommandAllowed("load_slot", slot))
   {
      return;
   }

   if (!PALPAL_AgentBeginSlotLoad("load_slot", slot)) return;
   PAL_ReloadInNextTick(slot);
   PALPAL_AgentWriteSlotResult(TRUE, "load_scheduled", "load_slot", slot, NULL);
}

static VOID
PALPAL_AgentDoStartSlot(
   int slot
)
{
   /*
    * A new game is an explicit reset, not a checkpoint load.  In particular,
    * the first new game immediately enters the opening dialog; rejecting a
    * later start request while that dialog is active left the Web UI unable to
    * start over again.  End the dialog and defer the reload to the main loop.
    */
   if (slot < 0)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "invalid_slot", "start_slot", slot, "invalid_slot");
      return;
   }
   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "not_in_main_game", "start_slot", slot, "not_in_main_game");
      return;
   }
   if (gpGlobals->fInBattle)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "in_battle", "start_slot", slot, "in_battle");
      return;
   }
   if (gpGlobals->fEnteringScene)
   {
      PALPAL_AgentWriteSlotResult(FALSE, "entering_scene", "start_slot", slot, "entering_scene");
      return;
   }

   PALPAL_AgentClearActionQueue();
   if (PAL_IsInDialog())
   {
      PAL_EndDialog();
   }
   if (!PALPAL_AgentBeginSlotLoad("start_slot", slot)) return;
   PAL_ReloadInNextTick(slot);
   PALPAL_AgentWriteSlotResult(TRUE, "start_scheduled", "start_slot", slot, NULL);
}

static BOOL
PALPAL_AgentPartyHasRole(
   WORD role_id
)
{
   int i;

   if (gpGlobals == NULL)
   {
      return FALSE;
   }
   for (i = 0; i <= (int)gpGlobals->wMaxPartyMemberIndex; i++)
   {
      if (gpGlobals->rgParty[i].wPlayerRole == role_id)
      {
         return TRUE;
      }
   }
   return FALSE;
}

static VOID
PALPAL_AgentUseItem(
   WORD item_id,
   WORD role_id,
   BOOL target_all
)
{
   WORD flags;
   BOOL consumed = FALSE;

   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      PALPAL_AgentWriteActionResult(FALSE, "not_in_main_game", item_id, role_id, target_all, FALSE, FALSE, "not_in_main_game");
      return;
   }
   if (gpGlobals->fInBattle)
   {
      PALPAL_AgentWriteActionResult(FALSE, "in_battle", item_id, role_id, target_all, FALSE, FALSE, "in_battle");
      return;
   }
   if (PAL_IsInDialog())
   {
      PALPAL_AgentWriteActionResult(FALSE, "in_dialog", item_id, role_id, target_all, FALSE, FALSE, "in_dialog");
      return;
   }
   if (g_agent_shop.active)
   {
      PALPAL_AgentWriteActionResult(FALSE, "in_shop", item_id, role_id, target_all, FALSE, FALSE, "in_shop");
      return;
   }
   if (item_id == 0 || item_id >= MAX_OBJECTS)
   {
      PALPAL_AgentWriteActionResult(FALSE, "invalid_item", item_id, role_id, target_all, FALSE, FALSE, "invalid_item");
      return;
   }
   if (PAL_GetItemAmount(item_id) <= 0)
   {
      PALPAL_AgentWriteActionResult(FALSE, "not_in_inventory", item_id, role_id, target_all, FALSE, FALSE, "not_in_inventory");
      return;
   }

   flags = gpGlobals->g.rgObject[item_id].item.wFlags;
   if (!(flags & kItemFlagUsable))
   {
      PALPAL_AgentWriteActionResult(FALSE, "not_usable", item_id, role_id, target_all, FALSE, FALSE, "not_usable");
      return;
   }
   if ((flags & kItemFlagApplyToAll) && !target_all)
   {
      PALPAL_AgentWriteActionResult(FALSE, "target_must_be_all", item_id, role_id, target_all, FALSE, FALSE, "target_must_be_all");
      return;
   }
   if (!(flags & kItemFlagApplyToAll) && target_all)
   {
      PALPAL_AgentWriteActionResult(FALSE, "target_must_be_role", item_id, role_id, target_all, FALSE, FALSE, "target_must_be_role");
      return;
   }
   if (!target_all && !PALPAL_AgentPartyHasRole(role_id))
   {
      PALPAL_AgentWriteActionResult(FALSE, "role_not_in_party", item_id, role_id, target_all, FALSE, FALSE, "role_not_in_party");
      return;
   }

   if (target_all)
   {
      gpGlobals->g.rgObject[item_id].item.wScriptOnUse =
         PAL_RunTriggerScript(gpGlobals->g.rgObject[item_id].item.wScriptOnUse, 0xFFFF);
   }
   else
   {
      gpGlobals->g.rgObject[item_id].item.wScriptOnUse =
         PAL_RunTriggerScript(gpGlobals->g.rgObject[item_id].item.wScriptOnUse, role_id);
   }

   if ((flags & kItemFlagConsuming) && g_fScriptSuccess)
   {
      consumed = PAL_AddItemToInventory(item_id, -1) ? TRUE : FALSE;
   }
   PALPAL_AgentWriteActionResult(TRUE, "used", item_id, role_id, target_all, consumed, g_fScriptSuccess, NULL);
}

static VOID
PALPAL_AgentEquipItem(
   WORD item_id,
   WORD role_id
)
{
   WORD flags;
   WORD previous_item_id;
   int inventory_index;
   PALPALEQUIPMENTPREVIEW preview;

   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "not_in_main_game", item_id, role_id, -1, 0, "not_in_main_game");
      return;
   }
   if (gpGlobals->fInBattle)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "in_battle", item_id, role_id, -1, 0, "in_battle");
      return;
   }
   if (PAL_IsInDialog())
   {
      PALPAL_AgentWriteEquipResult(FALSE, "in_dialog", item_id, role_id, -1, 0, "in_dialog");
      return;
   }
   if (g_agent_shop.active)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "in_shop", item_id, role_id, -1, 0, "in_shop");
      return;
   }
   if (item_id == 0 || item_id >= MAX_OBJECTS)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "invalid_item", item_id, role_id, -1, 0, "invalid_item");
      return;
   }
   if (!PALPAL_AgentPartyHasRole(role_id))
   {
      PALPAL_AgentWriteEquipResult(FALSE, "role_not_in_party", item_id, role_id, -1, 0, "role_not_in_party");
      return;
   }
   flags = gpGlobals->g.rgObject[item_id].item.wFlags;
   if (!(flags & kItemFlagEquipable))
   {
      PALPAL_AgentWriteEquipResult(FALSE, "not_equipable", item_id, role_id, -1, 0, "not_equipable");
      return;
   }
   if (!(flags & (kItemFlagEquipableByPlayerRole_First << role_id)))
   {
      PALPAL_AgentWriteEquipResult(FALSE, "role_not_eligible", item_id, role_id, -1, 0, "role_not_eligible");
      return;
   }
   if (!PAL_GetItemIndexToInventory(item_id, &inventory_index) ||
      inventory_index >= MAX_INVENTORY ||
      gpGlobals->rgInventory[inventory_index].nAmount <=
         gpGlobals->rgInventory[inventory_index].nAmountInUse)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "not_in_inventory", item_id, role_id, -1, 0, "not_in_inventory");
      return;
   }

   preview = PALPAL_AgentPreviewEquipment(item_id);
   if (preview.slot_id < 0 || preview.slot_id >= MAX_PLAYER_EQUIPMENTS)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "invalid_equip_script", item_id, role_id, preview.slot_id, 0, "invalid_equip_script");
      return;
   }
   previous_item_id = gpGlobals->g.PlayerRoles.rgwEquipment[preview.slot_id][role_id];
   gpGlobals->wLastUnequippedItem = item_id;
   gpGlobals->g.rgObject[item_id].item.wScriptOnEquip =
      PAL_RunTriggerScript(gpGlobals->g.rgObject[item_id].item.wScriptOnEquip, role_id);
   if (gpGlobals->g.PlayerRoles.rgwEquipment[preview.slot_id][role_id] != item_id)
   {
      PALPAL_AgentWriteEquipResult(FALSE, "equip_not_applied", item_id, role_id,
         preview.slot_id, previous_item_id, "equip_not_applied");
      return;
   }
   PALPAL_AgentWriteEquipResult(TRUE, "equipped", item_id, role_id,
      preview.slot_id, previous_item_id, NULL);
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
PALPAL_AgentQueueButton(
   const char *name
)
{
   DWORD button = PALPAL_AgentButtonForName(name);
   PALDIRECTION dir = kDirUnknown;

   if (button == kKeyNone || g_agent_action_count >= PALPAL_AGENT_MAX_ACTION_STEPS)
   {
      return;
   }

   if (button & kKeyUp) dir = kDirNorth;
   else if (button & kKeyDown) dir = kDirSouth;
   else if (button & kKeyLeft) dir = kDirWest;
   else if (button & kKeyRight) dir = kDirEast;

   g_agent_action_queue[g_agent_action_count].buttons = button;
   g_agent_action_queue[g_agent_action_count].dir = dir;
   g_agent_action_count++;
}

static VOID
PALPAL_AgentQueueMenuButton(
   DWORD button
)
{
   if (button == kKeyNone || g_agent_action_count >= PALPAL_AGENT_MAX_ACTION_STEPS)
   {
      return;
   }
   g_agent_action_queue[g_agent_action_count].buttons = button;
   g_agent_action_queue[g_agent_action_count].dir = kDirUnknown;
   g_agent_action_count++;
}

static VOID
PALPAL_AgentQueueChoiceSelect(
   DWORD generation,
   int option_index
)
{
   if (!g_agent_choice.active)
   {
      PALPAL_AgentWriteChoiceResult(FALSE, "choice_inactive", generation,
         option_index, -1, "choice_inactive");
      return;
   }
   if (generation != g_agent_choice.generation)
   {
      PALPAL_AgentWriteChoiceResult(FALSE, "stale_choice_command", generation,
         option_index, g_agent_choice.selected_index, "stale_choice_command");
      return;
   }
   if (option_index < 0 || option_index > 1)
   {
      PALPAL_AgentWriteChoiceResult(FALSE, "invalid_choice_option", generation,
         option_index, g_agent_choice.selected_index, "invalid_choice_option");
      return;
   }
   if (g_agent_choice.command_pending)
   {
      PALPAL_AgentWriteChoiceResult(FALSE, "choice_action_pending", generation,
         option_index, g_agent_choice.selected_index, "choice_action_pending");
      return;
   }

   g_agent_choice.command_pending = TRUE;
   g_agent_choice.requested_index = option_index;
   if (!g_agent_choice.menu_started)
   {
      /* Dismiss the question text before the confirm menu is constructed. */
      PALPAL_AgentQueueMenuButton(kKeySearch);
   }
   if (g_agent_choice.selected_index != option_index)
   {
      PALPAL_AgentQueueMenuButton(option_index > g_agent_choice.selected_index
         ? kKeyRight : kKeyLeft);
   }
   PALPAL_AgentQueueMenuButton(kKeySearch);
}

static int
PALPAL_AgentShopItemIndex(
   WORD item_id
)
{
   int i;

   if (!g_agent_shop.active || gpGlobals == NULL)
   {
      return -1;
   }
   if (g_agent_shop.mode == kPalpalAgentShopModeSell)
   {
      for (i = 0; i < MAX_INVENTORY && gpGlobals->rgInventory[i].wItem != 0; i++)
      {
         if (gpGlobals->rgInventory[i].wItem == item_id)
         {
            return i;
         }
      }
      return -1;
   }
   if (g_agent_shop.store_id >= gpGlobals->g.nStore)
   {
      return -1;
   }
   for (i = 0; i < MAX_STORE_ITEM; i++)
   {
      WORD offered = gpGlobals->g.lprgStore[g_agent_shop.store_id].rgwItems[i];
      if (offered == 0)
      {
         break;
      }
      if (offered == item_id)
      {
         return i;
      }
   }
   return -1;
}

static VOID
PALPAL_AgentQueueShopBuy(
   WORD item_id
)
{
   int target_index;
   int steps;
   WORD price = 0;

   if (!g_agent_shop.active || g_agent_shop.mode != kPalpalAgentShopModeBuy)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_inactive", "shop_buy", 0, item_id,
         0, 0, 0, 0, 0, "shop_inactive");
      return;
   }
   if (g_agent_shop.command != kPalpalAgentShopCommandNone)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_action_pending", "shop_buy",
         g_agent_shop.store_id, item_id, 0, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, "shop_action_pending");
      return;
   }
   target_index = PALPAL_AgentShopItemIndex(item_id);
   if (target_index < 0)
   {
      PALPAL_AgentWriteShopResult(FALSE, "item_not_offered", "shop_buy",
         g_agent_shop.store_id, item_id, 0, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, "item_not_offered");
      return;
   }
   price = gpGlobals->g.rgObject[item_id].item.wPrice;
   if (price > gpGlobals->dwCash)
   {
      PALPAL_AgentWriteShopResult(FALSE, "insufficient_cash", "shop_buy",
         g_agent_shop.store_id, item_id, price, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, "insufficient_cash");
      return;
   }

   g_agent_shop.command = kPalpalAgentShopCommandBuy;
   g_agent_shop.command_item_id = item_id;
   if (g_agent_shop.phase == kPalpalAgentShopPhaseConfirm)
   {
      if (g_agent_shop.selected_item_id == item_id)
      {
         PALPAL_AgentQueueMenuButton(kKeyDown);
         PALPAL_AgentQueueMenuButton(kKeySearch);
         return;
      }
      PALPAL_AgentQueueMenuButton(kKeyMenu);
   }

   steps = (target_index - g_agent_shop.selected_index + g_agent_shop.item_count) %
      g_agent_shop.item_count;
   while (steps-- > 0)
   {
      PALPAL_AgentQueueMenuButton(kKeyDown);
   }
   PALPAL_AgentQueueMenuButton(kKeySearch);
   PALPAL_AgentQueueMenuButton(kKeyDown);
   PALPAL_AgentQueueMenuButton(kKeySearch);
}

static VOID
PALPAL_AgentQueueShopSell(
   WORD item_id
)
{
   int target_index;
   int columns;
   int steps;
   WORD flags;

   if (!g_agent_shop.active || g_agent_shop.mode != kPalpalAgentShopModeSell)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_inactive", "shop_sell", 0, item_id,
         0, 0, 0, 0, 0, "shop_inactive");
      return;
   }
   if (g_agent_shop.command != kPalpalAgentShopCommandNone)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_action_pending", "shop_sell", 0,
         item_id, 0, gpGlobals->dwCash, gpGlobals->dwCash, 0, 0,
         "shop_action_pending");
      return;
   }
   target_index = PALPAL_AgentShopItemIndex(item_id);
   flags = item_id < MAX_OBJECTS ? gpGlobals->g.rgObject[item_id].item.wFlags : 0;
   if (target_index < 0 || !(flags & kItemFlagSellable) ||
      gpGlobals->rgInventory[target_index].nAmount <=
         gpGlobals->rgInventory[target_index].nAmountInUse)
   {
      PALPAL_AgentWriteShopResult(FALSE, "item_not_sellable", "shop_sell", 0,
         item_id, 0, gpGlobals->dwCash, gpGlobals->dwCash, 0, 0,
         "item_not_sellable");
      return;
   }

   g_agent_shop.command = kPalpalAgentShopCommandSell;
   g_agent_shop.command_item_id = item_id;
   if (g_agent_shop.phase == kPalpalAgentShopPhaseConfirm)
   {
      if (g_agent_shop.selected_item_id == item_id)
      {
         PALPAL_AgentQueueMenuButton(kKeyDown);
         PALPAL_AgentQueueMenuButton(kKeySearch);
         return;
      }
      PALPAL_AgentQueueMenuButton(kKeyMenu);
   }

   columns = 32 / gConfig.dwWordLength;
   if (columns < 1)
   {
      columns = 1;
   }
   PALPAL_AgentQueueMenuButton(kKeyHome);
   steps = target_index / columns;
   while (steps-- > 0)
   {
      PALPAL_AgentQueueMenuButton(kKeyDown);
   }
   steps = target_index % columns;
   while (steps-- > 0)
   {
      PALPAL_AgentQueueMenuButton(kKeyRight);
   }
   PALPAL_AgentQueueMenuButton(kKeySearch);
   PALPAL_AgentQueueMenuButton(kKeyDown);
   PALPAL_AgentQueueMenuButton(kKeySearch);
}

static VOID
PALPAL_AgentQueueShopLeave(
   VOID
)
{
   if (!g_agent_shop.active)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_inactive", "shop_leave", 0, 0,
         0, 0, 0, 0, 0, "shop_inactive");
      return;
   }
   if (g_agent_shop.command != kPalpalAgentShopCommandNone)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_action_pending", "shop_leave",
         g_agent_shop.mode == kPalpalAgentShopModeBuy ? g_agent_shop.store_id : 0,
         0, 0, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, "shop_action_pending");
      return;
   }

   g_agent_shop.command = kPalpalAgentShopCommandLeave;
   g_agent_shop.command_item_id = 0;
   if (g_agent_shop.phase == kPalpalAgentShopPhaseConfirm)
   {
      PALPAL_AgentQueueMenuButton(kKeyMenu);
   }
   PALPAL_AgentQueueMenuButton(kKeyMenu);
}

static BOOL
PALPAL_AgentPartyPosition(
   int *x,
   int *y
)
{
   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      return FALSE;
   }

   *x = PAL_X(gpGlobals->viewport) + PAL_X(gpGlobals->partyoffset);
   *y = PAL_Y(gpGlobals->viewport) + PAL_Y(gpGlobals->partyoffset);
   return TRUE;
}

static VOID
PALPAL_AgentClearActionQueue(
   VOID
)
{
   g_agent_action_pending = FALSE;
   g_agent_action_count = 0;
   g_agent_action_index = 0;
   g_agent_waiting_for_move = FALSE;
   g_agent_last_action_ticks = 0;
}

static int
PALPAL_AgentRemainingActionSteps(
   VOID
)
{
   int remaining = g_agent_action_count - g_agent_action_index;
   if (g_agent_waiting_for_move)
   {
      remaining++;
   }
   return remaining > 0 ? remaining : 0;
}

static VOID
PALPAL_AgentLoadActionFile(
   VOID
)
{
   char path[PAL_MAX_PATH];
   FILE *fp;
   char line[128];
   BOOL pending_use_item = FALSE;
   BOOL pending_equip_item = FALSE;
   BOOL pending_shop_buy = FALSE;
   BOOL pending_shop_sell = FALSE;
   BOOL pending_shop_leave = FALSE;
   BOOL pending_battle_action = FALSE;
   BOOL pending_choice_select = FALSE;
   BOOL pending_target_all = FALSE;
   WORD pending_item_id = 0;
   WORD pending_role_id = 0;
   WORD pending_equip_item_id = 0;
   WORD pending_equip_role_id = 0;
   PALPALAGENTSLOTCOMMAND pending_slot_command = kPalpalAgentSlotCommandNone;
   int pending_slot = 0;
   DWORD pending_battle_generation = 0;
   DWORD pending_command_generation = 0;
   WORD pending_battle_player = 0;
   WORD pending_battle_object = 0;
   SHORT pending_battle_target = -2;
   char pending_battle_type[32];
   DWORD pending_choice_generation = 0;
   int pending_choice_index = -1;

   pending_battle_type[0] = '\0';

   PALPAL_AgentPath(path, sizeof(path), "action.txt");
   fp = fopen(path, "r");
   if (!fp)
   {
      return;
   }

   g_agent_action_count = 0;
   g_agent_action_index = 0;
   g_agent_last_action_ticks = 0;
   g_agent_waiting_for_move = FALSE;
   g_agent_action_pending = FALSE;

   while (fgets(line, sizeof(line), fp))
   {
      char key[32], value[64];
      char target[64];
      if (sscanf(line, "%31s %63s", key, value) != 2)
      {
         continue;
      }
      if (strcmp(key, "request_id") == 0)
      {
         strncpy(g_agent_slot_request_id, value, sizeof(g_agent_slot_request_id) - 1);
         g_agent_slot_request_id[32] = '\0';
         continue;
      }
      if (strcmp(key, "frames") == 0)
      {
         continue;
      }
      else if (strcmp(key, "button") == 0)
      {
         PALPAL_AgentQueueButton(value);
      }
      else if (strcmp(key, "use_item") == 0)
      {
         BOOL target_all = FALSE;
         WORD role_id = 0;
         WORD item_id = (WORD)atoi(value);
         target[0] = '\0';
         if (sscanf(line, "%31s %63s %63s", key, value, target) == 3)
         {
            if (strcmp(target, "all") == 0)
            {
               target_all = TRUE;
            }
            else
            {
               role_id = (WORD)atoi(target);
            }
         }
         pending_use_item = TRUE;
         pending_item_id = item_id;
         pending_role_id = role_id;
         pending_target_all = target_all;
      }
      else if (strcmp(key, "equip_item") == 0)
      {
         WORD item_id = (WORD)atoi(value);
         target[0] = '\0';
         if (sscanf(line, "%31s %63s %63s", key, value, target) == 3)
         {
            pending_equip_item = TRUE;
            pending_equip_item_id = item_id;
            pending_equip_role_id = (WORD)atoi(target);
         }
      }
      else if (strcmp(key, "shop_buy") == 0)
      {
         pending_shop_buy = TRUE;
         pending_item_id = (WORD)atoi(value);
      }
      else if (strcmp(key, "shop_sell") == 0)
      {
         pending_shop_sell = TRUE;
         pending_item_id = (WORD)atoi(value);
      }
      else if (strcmp(key, "shop_leave") == 0)
      {
         pending_shop_leave = TRUE;
      }
      else if (strcmp(key, "battle_action") == 0)
      {
         unsigned int battle_generation, command_generation;
         unsigned int player_index, object_id;
         int target_index;
         char action_type[32];
         if (sscanf(line, "%31s %u %u %u %31s %u %d",
               key, &battle_generation, &command_generation, &player_index,
               action_type, &object_id, &target_index) == 7)
         {
            pending_battle_action = TRUE;
            pending_battle_generation = (DWORD)battle_generation;
            pending_command_generation = (DWORD)command_generation;
            pending_battle_player = (WORD)player_index;
            pending_battle_object = (WORD)object_id;
            pending_battle_target = (SHORT)target_index;
            strncpy(pending_battle_type, action_type, sizeof(pending_battle_type) - 1);
            pending_battle_type[sizeof(pending_battle_type) - 1] = '\0';
         }
      }
      else if (strcmp(key, "choice_select") == 0)
      {
         unsigned int generation;
         int option_index;
         if (sscanf(line, "%31s %u %d", key, &generation, &option_index) == 3)
         {
            pending_choice_select = TRUE;
            pending_choice_generation = (DWORD)generation;
            pending_choice_index = option_index;
         }
      }
      else if (strcmp(key, "save_slot") == 0)
      {
         pending_slot_command = kPalpalAgentSlotCommandSave;
         pending_slot = atoi(value);
      }
      else if (strcmp(key, "load_slot") == 0)
      {
         pending_slot_command = kPalpalAgentSlotCommandLoad;
         pending_slot = atoi(value);
      }
      else if (strcmp(key, "start_slot") == 0)
      {
         pending_slot_command = kPalpalAgentSlotCommandStart;
         pending_slot = atoi(value);
      }
   }

   fclose(fp);
   if (remove(path) != 0)
   {
      PALPAL_AgentClearActionQueue();
      return;
   }
   g_agent_action_generation++;
   if (pending_choice_select)
   {
      PALPAL_AgentQueueChoiceSelect(pending_choice_generation, pending_choice_index);
   }
   else if (pending_battle_action)
   {
      const char *status;
      if (strcmp(pending_battle_type, "continue") == 0)
      {
         if (!gpGlobals->fInBattle ||
             pending_battle_generation != g_agent_battle.generation ||
             pending_command_generation != g_agent_battle.command_generation)
         {
            status = "stale_battle_command";
         }
         else if (!g_agent_battle.awaiting_ack ||
                  g_agent_battle.phase != kPalpalAgentBattleReward)
         {
            status = "battle_not_awaiting_ack";
         }
         else
         {
            PALPAL_AgentQueueButton("search");
            g_agent_battle.awaiting_ack = FALSE;
            status = NULL;
         }
      }
      else
      {
         status = PALPAL_BattleSubmitAction(
            pending_battle_generation,
            pending_command_generation,
            pending_battle_player,
            pending_battle_type,
            pending_battle_object,
            pending_battle_target);
         if (status == NULL)
         {
            g_agent_battle.phase = kPalpalAgentBattleResolving;
         }
      }
      PALPAL_AgentWriteBattleResult(
         status == NULL,
         status == NULL ? "submitted" : status,
         pending_battle_generation,
         pending_command_generation,
         pending_battle_player,
         pending_battle_type,
         pending_battle_object,
         pending_battle_target);
   }
   else if (pending_shop_buy)
   {
      PALPAL_AgentQueueShopBuy(pending_item_id);
   }
   else if (pending_shop_sell)
   {
      PALPAL_AgentQueueShopSell(pending_item_id);
   }
   else if (pending_shop_leave)
   {
      PALPAL_AgentQueueShopLeave();
   }
   g_agent_action_pending = (g_agent_action_count > 0);
   if (pending_slot_command == kPalpalAgentSlotCommandSave)
   {
      PALPAL_AgentDoSaveSlot(pending_slot);
   }
   else if (pending_slot_command == kPalpalAgentSlotCommandLoad)
   {
      PALPAL_AgentDoLoadSlot(pending_slot);
   }
   else if (pending_slot_command == kPalpalAgentSlotCommandStart)
   {
      PALPAL_AgentDoStartSlot(pending_slot);
   }
   if (pending_use_item)
   {
      PALPAL_AgentUseItem(pending_item_id, pending_role_id, pending_target_all);
   }
   else if (pending_equip_item)
   {
      PALPAL_AgentEquipItem(pending_equip_item_id, pending_equip_role_id);
   }
}

VOID
PALPAL_AgentPrepareChoice(
   WORD wEventObjectID,
   WORD wScriptEntry
)
{
   DWORD generation = g_agent_choice.generation + 1;
   if (!PALPAL_AgentEnabled()) return;
   if (g_agent_choice.active &&
       g_agent_choice.event_object_id == wEventObjectID &&
       g_agent_choice.script_entry == wScriptEntry)
   {
      return;
   }
   memset(&g_agent_choice, 0, sizeof(g_agent_choice));
   g_agent_choice.active = TRUE;
   g_agent_choice.generation = generation ? generation : 1;
   g_agent_choice.event_object_id = wEventObjectID;
   g_agent_choice.script_entry = wScriptEntry;
   g_agent_choice.selected_index = 0;
   g_agent_choice.requested_index = -1;
}

VOID
PALPAL_AgentBeginChoice(
   WORD wEventObjectID,
   WORD wScriptEntry
)
{
   PALPAL_AgentPrepareChoice(wEventObjectID, wScriptEntry);
   if (g_agent_choice.active &&
       g_agent_choice.event_object_id == wEventObjectID &&
       g_agent_choice.script_entry == wScriptEntry)
   {
      g_agent_choice.menu_started = TRUE;
   }
}

VOID
PALPAL_AgentObserveChoiceSelection(
   WORD wSelectedIndex
)
{
   if (g_agent_choice.active && wSelectedIndex < 2)
   {
      g_agent_choice.selected_index = (int)wSelectedIndex;
   }
}

VOID
PALPAL_AgentEndChoice(
   WORD wSelectedIndex
)
{
   int selected_index;
   if (!g_agent_choice.active) return;
   selected_index = (wSelectedIndex == MENUITEM_VALUE_CANCELLED) ? 0 : (int)wSelectedIndex;
   g_agent_choice.selected_index = selected_index;
   if (g_agent_choice.command_pending)
   {
      BOOL matched = selected_index == g_agent_choice.requested_index;
      PALPAL_AgentWriteChoiceResult(matched,
         matched ? "selected" : "choice_selection_mismatch",
         g_agent_choice.generation, g_agent_choice.requested_index,
         selected_index, matched ? NULL : "choice_selection_mismatch");
   }
   g_agent_choice.active = FALSE;
   g_agent_choice.command_pending = FALSE;
}

VOID
PALPAL_AgentBeginBuyMenu(
   WORD wStoreNum,
   WORD wSelectedItem
)
{
   int i;

   memset(&g_agent_shop, 0, sizeof(g_agent_shop));
   if (!PALPAL_AgentEnabled() || gpGlobals == NULL || wStoreNum >= gpGlobals->g.nStore)
   {
      return;
   }
   g_agent_shop.active = TRUE;
   g_agent_shop.mode = kPalpalAgentShopModeBuy;
   g_agent_shop.store_id = wStoreNum;
   g_agent_shop.selected_item_id = wSelectedItem;
   g_agent_shop.selected_index = 0;
   g_agent_shop.phase = kPalpalAgentShopPhaseSelect;
   for (i = 0; i < MAX_STORE_ITEM; i++)
   {
      if (gpGlobals->g.lprgStore[wStoreNum].rgwItems[i] == 0)
      {
         break;
      }
   }
   g_agent_shop.item_count = i;
}

VOID
PALPAL_AgentBeginSellMenu(
   VOID
)
{
   int i;

   memset(&g_agent_shop, 0, sizeof(g_agent_shop));
   if (!PALPAL_AgentEnabled() || gpGlobals == NULL)
   {
      return;
   }
   PAL_CompressInventory();
   g_agent_shop.active = TRUE;
   g_agent_shop.mode = kPalpalAgentShopModeSell;
   g_agent_shop.phase = kPalpalAgentShopPhaseSelect;
   for (i = 0; i < MAX_INVENTORY && gpGlobals->rgInventory[i].wItem != 0; i++)
   {
   }
   g_agent_shop.item_count = i;
   if (i > 0)
   {
      if (gpGlobals->iCurInvMenuItem < 0 || gpGlobals->iCurInvMenuItem >= i)
      {
         gpGlobals->iCurInvMenuItem = 0;
      }
      g_agent_shop.selected_index = gpGlobals->iCurInvMenuItem;
      g_agent_shop.selected_item_id =
         gpGlobals->rgInventory[g_agent_shop.selected_index].wItem;
   }
}

VOID
PALPAL_AgentObserveBuyMenuSelection(
   WORD wSelectedItem
)
{
   int index;

   if (!g_agent_shop.active)
   {
      return;
   }
   index = PALPAL_AgentShopItemIndex(wSelectedItem);
   if (index >= 0)
   {
      g_agent_shop.selected_item_id = wSelectedItem;
      g_agent_shop.selected_index = index;
   }
}

VOID
PALPAL_AgentObserveSellMenuSelection(
   WORD wSelectedItem
)
{
   int i;
   int index;

   if (!g_agent_shop.active || g_agent_shop.mode != kPalpalAgentShopModeSell)
   {
      return;
   }
   for (i = 0; i < MAX_INVENTORY && gpGlobals->rgInventory[i].wItem != 0; i++)
   {
   }
   g_agent_shop.item_count = i;
   index = PALPAL_AgentShopItemIndex(wSelectedItem);
   if (index >= 0)
   {
      g_agent_shop.selected_item_id = wSelectedItem;
      g_agent_shop.selected_index = index;
   }
}

VOID
PALPAL_AgentObserveBuyMenuPhase(
   BOOL fConfirming
)
{
   if (g_agent_shop.active)
   {
      g_agent_shop.phase = fConfirming
         ? kPalpalAgentShopPhaseConfirm
         : kPalpalAgentShopPhaseSelect;
   }
}

VOID
PALPAL_AgentObserveSellMenuPhase(
   BOOL fConfirming
)
{
   if (g_agent_shop.active && g_agent_shop.mode == kPalpalAgentShopModeSell)
   {
      g_agent_shop.phase = fConfirming
         ? kPalpalAgentShopPhaseConfirm
         : kPalpalAgentShopPhaseSelect;
   }
}

VOID
PALPAL_AgentObserveBuyMenuResult(
   WORD wItem,
   BOOL fPurchased,
   DWORD dwCashBefore,
   DWORD dwCashAfter,
   INT iOwnedBefore,
   INT iOwnedAfter
)
{
   WORD price;

   if (!g_agent_shop.active ||
      g_agent_shop.command != kPalpalAgentShopCommandBuy ||
      g_agent_shop.command_item_id != wItem)
   {
      return;
   }
   price = gpGlobals->g.rgObject[wItem].item.wPrice;
   PALPAL_AgentWriteShopResult(
      fPurchased,
      fPurchased ? "purchased" : "purchase_cancelled",
      "shop_buy",
      g_agent_shop.store_id,
      wItem,
      price,
      dwCashBefore,
      dwCashAfter,
      iOwnedBefore,
      iOwnedAfter,
      fPurchased ? NULL : "purchase_cancelled");
   g_agent_shop.command = kPalpalAgentShopCommandNone;
   g_agent_shop.command_item_id = 0;
}

VOID
PALPAL_AgentObserveSellMenuResult(
   WORD wItem,
   BOOL fSold,
   DWORD dwCashBefore,
   DWORD dwCashAfter,
   INT iOwnedBefore,
   INT iOwnedAfter
)
{
   WORD price;

   if (!g_agent_shop.active ||
      g_agent_shop.mode != kPalpalAgentShopModeSell ||
      g_agent_shop.command != kPalpalAgentShopCommandSell ||
      g_agent_shop.command_item_id != wItem)
   {
      return;
   }
   price = gpGlobals->g.rgObject[wItem].item.wPrice / 2;
   PALPAL_AgentWriteShopResult(
      fSold,
      fSold ? "sold" : "sale_cancelled",
      "shop_sell",
      0,
      wItem,
      price,
      dwCashBefore,
      dwCashAfter,
      iOwnedBefore,
      iOwnedAfter,
      fSold ? NULL : "sale_cancelled");
   g_agent_shop.command = kPalpalAgentShopCommandNone;
   g_agent_shop.command_item_id = 0;
}

VOID
PALPAL_AgentEndBuyMenu(
   VOID
)
{
   if (!g_agent_shop.active)
   {
      return;
   }
   if (g_agent_shop.command == kPalpalAgentShopCommandLeave)
   {
      PALPAL_AgentWriteShopResult(TRUE, "left", "shop_leave",
         g_agent_shop.store_id, 0, 0, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, NULL);
   }
   else if (g_agent_shop.command == kPalpalAgentShopCommandBuy)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_closed", "shop_buy",
         g_agent_shop.store_id, g_agent_shop.command_item_id, 0,
         gpGlobals->dwCash, gpGlobals->dwCash, 0, 0, "shop_closed");
   }
   memset(&g_agent_shop, 0, sizeof(g_agent_shop));
}

VOID
PALPAL_AgentEndSellMenu(
   VOID
)
{
   if (!g_agent_shop.active || g_agent_shop.mode != kPalpalAgentShopModeSell)
   {
      return;
   }
   if (g_agent_shop.command == kPalpalAgentShopCommandLeave)
   {
      PALPAL_AgentWriteShopResult(TRUE, "left", "shop_leave", 0, 0, 0,
         gpGlobals->dwCash, gpGlobals->dwCash, 0, 0, NULL);
   }
   else if (g_agent_shop.command == kPalpalAgentShopCommandSell)
   {
      PALPAL_AgentWriteShopResult(FALSE, "shop_closed", "shop_sell", 0,
         g_agent_shop.command_item_id, 0, gpGlobals->dwCash, gpGlobals->dwCash,
         0, 0, "shop_closed");
   }
   memset(&g_agent_shop, 0, sizeof(g_agent_shop));
}

VOID
PALPAL_AgentBattleHPRedistributed(VOID)
{
   g_agent_battle.hp_tracking_safe = FALSE;
}

VOID
PALPAL_AgentBattleBegin(
   VOID
)
{
   DWORD generation = g_agent_battle.generation + 1;
   int i;

   memset(&g_agent_battle, 0, sizeof(g_agent_battle));
   memset(g_agent_visible_outcomes, 0, sizeof(g_agent_visible_outcomes));
   memset(&g_agent_visible_active, 0, sizeof(g_agent_visible_active));
   g_agent_visible_outcome_count = 0;
   g_agent_visible_sequence = 0;
   g_agent_battle.generation = generation ? generation : 1;
   g_agent_battle.hp_tracking_safe = TRUE;
   g_agent_battle.phase = kPalpalAgentBattlePreBattle;
   g_agent_battle.started_ticks = SDL_GetTicks();
   if (gpGlobals == NULL)
   {
      return;
   }
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      g_agent_battle.enemy_object_id[i] = g_Battle.rgEnemy[i].wObjectID;
      g_agent_battle.enemy_max_hp[i] = g_Battle.rgEnemy[i].e.wHealth;
   }
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      if (i <= gpGlobals->wMaxPartyMemberIndex)
      {
         WORD role = gpGlobals->rgParty[i].wPlayerRole;
         g_agent_battle.player_role[i] = role;
         g_agent_battle.player_hp_before[i] = gpGlobals->g.PlayerRoles.rgwHP[role];
         g_agent_battle.player_mp_before[i] = gpGlobals->g.PlayerRoles.rgwMP[role];
         g_agent_battle.player_level_before[i] = gpGlobals->g.PlayerRoles.rgwLevel[role];
         memcpy(g_agent_battle.player_status_before[i],
            gpGlobals->rgPlayerStatus[role],
            sizeof(g_agent_battle.player_status_before[i]));
         {
            int j;
            for (j = 0; j < MAX_PLAYER_MAGICS; j++)
            {
               g_agent_battle.player_magics_before[i][j] =
                  gpGlobals->g.PlayerRoles.rgwMagic[j][role];
            }
         }
      }
      else
      {
         g_agent_battle.player_role[i] = 0xFFFF;
      }
   }
}

VOID
PALPAL_AgentVisibleActionBegin(
   BOOL fPlayer,
   WORD wActorIndex,
   INT iActionType,
   WORD wObjectID,
   SHORT sTarget
)
{
   int i;
   if (!PALPAL_AgentEnabled() || !gpGlobals || !gpGlobals->fInBattle)
      return;
   if (g_agent_visible_active.valid)
      PALPAL_AgentVisibleActionEnd();
   memset(&g_agent_visible_active, 0, sizeof(g_agent_visible_active));
   g_agent_visible_active.valid = TRUE;
   g_agent_visible_active.sequence = ++g_agent_visible_sequence;
   g_agent_visible_active.player_actor = fPlayer;
   g_agent_visible_active.actor_index = wActorIndex;
   g_agent_visible_active.action_type = iActionType;
   g_agent_visible_active.object_id = fPlayer ? wObjectID : 0;
   g_agent_visible_active.target_index = sTarget;
   g_agent_visible_active.mp_cost = 0;
   if (fPlayer && wActorIndex <= gpGlobals->wMaxPartyMemberIndex)
   {
      WORD role = gpGlobals->rgParty[wActorIndex].wPlayerRole;
      g_agent_visible_active.actor_role = role;
      if (iActionType == kBattleActionMagic && wObjectID > 0 && wObjectID < MAX_OBJECTS)
      {
         WORD number = gpGlobals->g.rgObject[wObjectID].magic.wMagicNumber;
         if (number < gpGlobals->g.nMagic)
            g_agent_visible_active.mp_cost = gpGlobals->g.lprgMagic[number].wCostMP;
      }
      if (sTarget >= -1 && sTarget <= g_Battle.wMaxEnemyIndex)
      {
         BOOL enemy_target = iActionType == kBattleActionAttack ||
            iActionType == kBattleActionThrowItem ||
            iActionType == kBattleActionCoopMagic;
         if (iActionType == kBattleActionMagic && wObjectID > 0 && wObjectID < MAX_OBJECTS)
            enemy_target = (gpGlobals->g.rgObject[wObjectID].magic.wFlags &
               kMagicFlagUsableToEnemy) != 0;
         g_agent_visible_active.enemy_target = enemy_target;
         if (enemy_target && sTarget >= 0)
            g_agent_visible_active.target_object_id = g_Battle.rgEnemy[sTarget].wObjectID;
      }
   }
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      if (i <= gpGlobals->wMaxPartyMemberIndex)
      {
         WORD role = gpGlobals->rgParty[i].wPlayerRole;
         g_agent_visible_active.player_hp_before[i] = gpGlobals->g.PlayerRoles.rgwHP[role];
         g_agent_visible_active.player_mp_before[i] = gpGlobals->g.PlayerRoles.rgwMP[role];
         g_agent_visible_active.player_hp_max[i] = gpGlobals->g.PlayerRoles.rgwMaxHP[role];
         g_agent_visible_active.player_mp_max[i] = gpGlobals->g.PlayerRoles.rgwMaxMP[role];
         memcpy(g_agent_visible_active.player_status_before[i], gpGlobals->rgPlayerStatus[role],
            sizeof(g_agent_visible_active.player_status_before[i]));
         {
            int j;
            for (j = 0; j < MAX_POISONS; j++)
               g_agent_visible_active.player_poison_before[i][j] =
                  gpGlobals->rgPoisonStatus[j][i].wPoisonID;
         }
      }
   }
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      g_agent_visible_active.enemy_object_id[i] = g_Battle.rgEnemy[i].wObjectID;
      g_agent_visible_active.enemy_hp_before[i] = g_Battle.rgEnemy[i].e.wHealth;
   }
}

VOID
PALPAL_AgentVisibleActionTarget(SHORT sTarget)
{
   if (!g_agent_visible_active.valid || !g_agent_visible_active.player_actor)
      return;
   g_agent_visible_active.target_index = sTarget;
   g_agent_visible_active.target_object_id = 0;
   if (g_agent_visible_active.enemy_target && sTarget >= 0 &&
       sTarget < MAX_ENEMIES_IN_TEAM)
      g_agent_visible_active.target_object_id = g_Battle.rgEnemy[sTarget].wObjectID;
}

VOID
PALPAL_AgentVisibleRoundEndBegin(
   VOID
)
{
   PALPAL_AgentVisibleActionBegin(FALSE, 0xFFFF, 0, 0, -1);
   if (g_agent_visible_active.valid)
      g_agent_visible_active.round_end = TRUE;
}

VOID
PALPAL_AgentVisibleActionEnd(
   VOID
)
{
   int i, index;
   PALPALAGENTVISIBLEOUTCOME *outcome;
   if (!g_agent_visible_active.valid || !gpGlobals)
      return;
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      if (i <= gpGlobals->wMaxPartyMemberIndex)
      {
         WORD role = gpGlobals->rgParty[i].wPlayerRole;
         g_agent_visible_active.player_hp_after[i] = gpGlobals->g.PlayerRoles.rgwHP[role];
         g_agent_visible_active.player_mp_after[i] = gpGlobals->g.PlayerRoles.rgwMP[role];
         memcpy(g_agent_visible_active.player_status_after[i], gpGlobals->rgPlayerStatus[role],
            sizeof(g_agent_visible_active.player_status_after[i]));
         {
            int j;
            for (j = 0; j < MAX_POISONS; j++)
               g_agent_visible_active.player_poison_after[i][j] =
                  gpGlobals->rgPoisonStatus[j][i].wPoisonID;
         }
      }
   }
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
      g_agent_visible_active.enemy_hp_after[i] = g_Battle.rgEnemy[i].e.wHealth;
   if (g_agent_visible_outcome_count < PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY)
   {
      index = g_agent_visible_outcome_count++;
   }
   else
   {
      memmove(&g_agent_visible_outcomes[0], &g_agent_visible_outcomes[1],
         sizeof(g_agent_visible_outcomes[0]) * (PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY - 1));
      index = PALPAL_AGENT_VISIBLE_OUTCOME_CAPACITY - 1;
   }
   outcome = &g_agent_visible_outcomes[index];
   memcpy(outcome, &g_agent_visible_active, sizeof(*outcome));
   memset(&g_agent_visible_active, 0, sizeof(g_agent_visible_active));
}

VOID
PALPAL_AgentVisibleNumber(
   BOOL fEnemy,
   WORD wTargetIndex,
   BOOL fMP,
   SHORT sChange
)
{
   int index;
   if (!g_agent_visible_active.valid || sChange == 0 ||
       g_agent_visible_active.number_count >= PALPAL_AGENT_VISIBLE_NUMBER_CAPACITY)
      return;
   index = g_agent_visible_active.number_count++;
   g_agent_visible_active.numbers[index].enemy = fEnemy;
   g_agent_visible_active.numbers[index].target_index = wTargetIndex;
   g_agent_visible_active.numbers[index].mp = fMP;
   g_agent_visible_active.numbers[index].change = sChange;
}

VOID
PALPAL_AgentBattleCommandReady(
   WORD wPlayerIndex
)
{
   if (!PALPAL_AgentEnabled() || !gpGlobals || !gpGlobals->fInBattle)
   {
      return;
   }
   g_agent_battle.command_generation++;
   if (g_agent_battle.command_generation == 0)
   {
      g_agent_battle.command_generation = 1;
   }
   g_agent_battle.phase = kPalpalAgentBattleCommand;
   g_agent_battle.awaiting_ack = FALSE;
   g_agent_battle.current_player_index = wPlayerIndex;
}

VOID
PALPAL_AgentBattleRewardBegin(
   VOID
)
{
   if (g_agent_battle.generation)
   {
      g_agent_battle.command_generation++;
      g_agent_battle.phase = kPalpalAgentBattleReward;
      g_agent_battle.awaiting_ack = TRUE;
   }
}

VOID
PALPAL_AgentBattleEnding(
   VOID
)
{
   int i, j;
   if (g_agent_battle.generation)
   {
      g_agent_battle.phase = kPalpalAgentBattleEnding;
      g_agent_battle.awaiting_ack = FALSE;
      for (i = 0; gpGlobals && i < MAX_PLAYERS_IN_PARTY; i++)
      {
         WORD role = g_agent_battle.player_role[i];
         if (role == 0xFFFF || role >= MAX_PLAYER_ROLES) continue;
         memcpy(g_agent_battle.player_status_after[i],
            gpGlobals->rgPlayerStatus[role],
            sizeof(g_agent_battle.player_status_after[i]));
         for (j = 0; j < MAX_PLAYER_MAGICS; j++)
         {
            WORD magic = gpGlobals->g.PlayerRoles.rgwMagic[j][role];
            int k, known = 0;
            if (magic == 0) continue;
            for (k = 0; k < MAX_PLAYER_MAGICS; k++)
            {
               if (g_agent_battle.player_magics_before[i][k] == magic)
               {
                  known = 1;
                  break;
               }
            }
            if (!known)
            {
               g_agent_battle.player_new_magics[i][j] = magic;
            }
         }
      }
   }
}

VOID
PALPAL_AgentBattleEnd(
   INT iResult
)
{
   int i;

   PALPAL_AgentVisibleActionEnd();

   memset(&g_agent_last_battle, 0, sizeof(g_agent_last_battle));
   g_agent_last_battle.valid = TRUE;
   g_agent_last_battle.hp_tracking_safe = g_agent_battle.hp_tracking_safe;
   g_agent_last_battle.generation = g_agent_battle.generation;
   g_agent_last_battle.result = iResult;
   g_agent_last_battle.is_boss = g_Battle.fIsBoss;
   g_agent_last_battle.ended_frame = gpGlobals ? gpGlobals->dwFrameNum : 0;
   g_agent_last_battle.duration_ms = SDL_GetTicks() - g_agent_battle.started_ticks;
   g_agent_last_battle.exp_gained = g_Battle.iExpGained;
   g_agent_last_battle.cash_gained = g_Battle.iCashGained;
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      g_agent_last_battle.enemy_object_id[i] = g_agent_battle.enemy_object_id[i];
   }
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      WORD role = g_agent_battle.player_role[i];
      g_agent_last_battle.player_role[i] = role;
      g_agent_last_battle.player_hp_before[i] = g_agent_battle.player_hp_before[i];
      g_agent_last_battle.player_mp_before[i] = g_agent_battle.player_mp_before[i];
      g_agent_last_battle.player_level_before[i] = g_agent_battle.player_level_before[i];
      memcpy(g_agent_last_battle.player_status_before[i],
         g_agent_battle.player_status_before[i],
         sizeof(g_agent_last_battle.player_status_before[i]));
      memcpy(g_agent_last_battle.player_status_after[i],
         g_agent_battle.player_status_after[i],
         sizeof(g_agent_last_battle.player_status_after[i]));
      memcpy(g_agent_last_battle.player_new_magics[i],
         g_agent_battle.player_new_magics[i],
         sizeof(g_agent_last_battle.player_new_magics[i]));
      if (gpGlobals && role != 0xFFFF && role < MAX_PLAYER_ROLES)
      {
         g_agent_last_battle.player_hp_after[i] = gpGlobals->g.PlayerRoles.rgwHP[role];
         g_agent_last_battle.player_mp_after[i] = gpGlobals->g.PlayerRoles.rgwMP[role];
         g_agent_last_battle.player_level_after[i] = gpGlobals->g.PlayerRoles.rgwLevel[role];
      }
   }
   g_agent_last_battle.visible_outcome_count = g_agent_visible_outcome_count;
   memcpy(g_agent_last_battle.visible_outcomes, g_agent_visible_outcomes,
      sizeof(g_agent_last_battle.visible_outcomes));
   g_agent_battle.phase = kPalpalAgentBattleNone;
   g_agent_battle.awaiting_ack = FALSE;
}

DWORD
PALPAL_AgentBattleGeneration(
   VOID
)
{
   return g_agent_battle.generation;
}

DWORD
PALPAL_AgentBattleCommandGeneration(
   VOID
)
{
   return g_agent_battle.command_generation;
}

BOOL
PALPAL_AgentBattleAwaitingAck(
   VOID
)
{
   return g_agent_battle.awaiting_ack;
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

   if (g_agent_startup_waiting)
   {
      if (!g_agent_startup_ready)
      {
         PALPAL_AgentLoadStartupActionFile();
      }
      return;
   }

   if (gpGlobals == NULL || !gpGlobals->fInMainGame)
   {
      return;
   }

   if (!g_agent_action_pending)
   {
      PALPAL_AgentLoadActionFile();
   }

   if (g_agent_action_pending)
   {
      DWORD now = SDL_GetTicks();
      int x = 0;
      int y = 0;

      if (g_agent_waiting_for_move)
      {
         if (PALPAL_AgentPartyPosition(&x, &y) && (x != g_agent_wait_x || y != g_agent_wait_y))
         {
            g_agent_waiting_for_move = FALSE;
         }
         else if (now - g_agent_wait_ticks >= 300)
         {
            PALPAL_AgentClearActionQueue();
            return;
         }
         else
         {
            return;
         }
      }
      if (g_agent_action_index >= g_agent_action_count)
      {
         PALPAL_AgentClearActionQueue();
         return;
      }

      PALPALACTIONSTEP *step = &g_agent_action_queue[g_agent_action_index];

      if (g_agent_last_action_ticks != 0 && now - g_agent_last_action_ticks < 30)
      {
         return;
      }

      g_InputState.dwKeyPress |= step->buttons;
      if (step->dir != kDirUnknown)
      {
         g_InputState.dir = step->dir;
         g_InputState.prevdir = kDirUnknown;
         g_agent_injected_dir = step->dir;
         g_agent_dir_active = TRUE;
      }
      if (step->dir != kDirUnknown && !gpGlobals->fInBattle &&
          PALPAL_AgentPartyPosition(&x, &y))
      {
         g_agent_waiting_for_move = TRUE;
         g_agent_wait_x = x;
         g_agent_wait_y = y;
         g_agent_wait_ticks = now;
      }
      g_agent_last_action_ticks = now;
      g_agent_action_index++;
      if (g_agent_action_index >= g_agent_action_count)
      {
         g_agent_action_pending = g_agent_waiting_for_move;
         if (!g_agent_action_pending)
         {
            g_agent_action_count = 0;
            g_agent_action_index = 0;
         }
      }
   }
}

VOID
PALPAL_AgentAfterFrame(
   VOID
)
{
   if (!PALPAL_AgentEnabled() || !g_agent_dir_active)
   {
      return;
   }

   if (g_InputState.dir == g_agent_injected_dir)
   {
      g_InputState.dir = kDirUnknown;
      g_InputState.prevdir = kDirUnknown;
   }
   g_agent_dir_active = FALSE;
   g_agent_injected_dir = kDirUnknown;
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

#define PALPAL_ROOM_GRID_W 128
#define PALPAL_ROOM_GRID_H 256
#define PALPAL_ROOM_GRID_SIZE (PALPAL_ROOM_GRID_W * PALPAL_ROOM_GRID_H)

static unsigned char g_agent_room_seen[PALPAL_ROOM_GRID_SIZE];
static unsigned short g_agent_room_queue_x[PALPAL_ROOM_GRID_SIZE];
static unsigned short g_agent_room_queue_y[PALPAL_ROOM_GRID_SIZE];

typedef struct tagPALPALROOMINFO
{
   int count;
   int min_x;
   int min_y;
   int max_x;
   int max_y;
   unsigned int hash;
} PALPALROOMINFO;

static int
PALPAL_AgentRoomIndex(
   int x,
   int y
)
{
   return y * PALPAL_ROOM_GRID_W + x;
}

static BOOL
PALPAL_AgentRoomNodeBlocked(
   int x,
   int y
)
{
   if (x < 0 || x >= PALPAL_ROOM_GRID_W || y < 0 || y >= PALPAL_ROOM_GRID_H)
   {
      return TRUE;
   }

   return PAL_CheckObstacleWithRange(PAL_XY(x * 16, y * 8), FALSE, 0, TRUE);
}

static VOID
PALPAL_AgentRoomHashNode(
   PALPALROOMINFO *info,
   int x,
   int y
)
{
   unsigned int value = (unsigned int)PALPAL_AgentRoomIndex(x, y);

   info->hash ^= value & 0xffu;
   info->hash *= 16777619u;
   info->hash ^= (value >> 8) & 0xffu;
   info->hash *= 16777619u;
}

static BOOL
PALPAL_AgentFindRoomStart(
   int *start_x,
   int *start_y
)
{
   int radius, dx, dy;

   if (!PALPAL_AgentRoomNodeBlocked(*start_x, *start_y))
   {
      return TRUE;
   }

   for (radius = 1; radius <= 4; radius++)
   {
      for (dy = -radius; dy <= radius; dy++)
      {
         for (dx = -radius; dx <= radius; dx++)
         {
            int x = *start_x + dx;
            int y = *start_y + dy;
            if (abs(dx) != radius && abs(dy) != radius)
            {
               continue;
            }
            /* Native steps change both grid axes by one. Never seed the
             * opposite parity lattice, which the player cannot step onto.
             * Radius one therefore checks only actual adjacent destinations.
             */
            if ((dx + dy) % 2 != 0)
            {
               continue;
            }
            if (!PALPAL_AgentRoomNodeBlocked(x, y))
            {
               *start_x = x;
               *start_y = y;
               return TRUE;
            }
         }
      }
   }

   return FALSE;
}

static PALPALROOMINFO
PALPAL_AgentComputeRoom(
   int player_x,
   int player_y
)
{
   static const int dx[4] = { 1, 1, -1, -1 };
   static const int dy[4] = { -1, 1, 1, -1 };
   PALPALROOMINFO info;
   int start_x = player_x / 16;
   int start_y = player_y / 8;
   int head = 0;
   int tail = 0;
   int i;

   memset(&info, 0, sizeof(info));
   info.min_x = info.min_y = 9999;
   info.max_x = info.max_y = -1;
   info.hash = 2166136261u;

   memset(g_agent_room_seen, 0, sizeof(g_agent_room_seen));

   if (!PALPAL_AgentFindRoomStart(&start_x, &start_y))
   {
      return info;
   }

   g_agent_room_seen[PALPAL_AgentRoomIndex(start_x, start_y)] = 1;
   g_agent_room_queue_x[tail] = (unsigned short)start_x;
   g_agent_room_queue_y[tail] = (unsigned short)start_y;
   tail++;

   while (head < tail)
   {
      int x = g_agent_room_queue_x[head];
      int y = g_agent_room_queue_y[head];
      head++;

      info.count++;
      if (x < info.min_x) info.min_x = x;
      if (y < info.min_y) info.min_y = y;
      if (x > info.max_x) info.max_x = x;
      if (y > info.max_y) info.max_y = y;

      for (i = 0; i < 4; i++)
      {
         int nx = x + dx[i];
         int ny = y + dy[i];
         int index;

         if (PALPAL_AgentRoomNodeBlocked(nx, ny))
         {
            continue;
         }

         index = PALPAL_AgentRoomIndex(nx, ny);
         if (g_agent_room_seen[index])
         {
            continue;
         }

         g_agent_room_seen[index] = 1;
         g_agent_room_queue_x[tail] = (unsigned short)nx;
         g_agent_room_queue_y[tail] = (unsigned short)ny;
         tail++;
      }
   }

   for (i = 0; i < PALPAL_ROOM_GRID_SIZE; i++)
   {
      if (g_agent_room_seen[i])
      {
         PALPAL_AgentRoomHashNode(&info, i % PALPAL_ROOM_GRID_W, i / PALPAL_ROOM_GRID_W);
      }
   }

   return info;
}

static VOID
PALPAL_AgentWriteRoom(
   FILE *fp,
   WORD scene_id,
   WORD map_id,
   int player_x,
   int player_y
)
{
   PALPALROOMINFO room = PALPAL_AgentComputeRoom(player_x, player_y);

   fprintf(fp,
      "\"room\":{\"key\":\"%u:%u:%d:%d:%d:%d:%d:%08x\","
      "\"hash\":%u,\"nodes\":%d,"
      "\"bounds\":{\"x\":%d,\"y\":%d,\"w\":%d,\"h\":%d}}",
      scene_id,
      map_id,
      room.min_x,
      room.min_y,
      room.max_x,
      room.max_y,
      room.count,
      room.hash,
      room.hash,
      room.count,
      room.min_x * 16,
      room.min_y * 8,
      (room.max_x - room.min_x + 1) * 16,
      (room.max_y - room.min_y + 1) * 8);
}

static VOID
PALPAL_AgentWriteNavGrid(
   FILE *fp,
   int player_x,
   int player_y
)
{
   PALPALROOMINFO room = PALPAL_AgentComputeRoom(player_x, player_y);
   int width;
   int height;
   int total;
   int i;

   if (room.count <= 0)
   {
      fprintf(fp,
         "\"nav_grid\":{\"origin\":{\"x\":0,\"y\":0},"
         "\"step\":{\"x\":16,\"y\":8},\"width\":0,\"height\":0,\"bits\":\"\"}");
      return;
   }

   width = room.max_x - room.min_x + 1;
   height = room.max_y - room.min_y + 1;
   total = width * height;

   fprintf(fp,
      "\"nav_grid\":{\"origin\":{\"x\":%d,\"y\":%d},"
      "\"step\":{\"x\":16,\"y\":8},\"width\":%d,\"height\":%d,"
      "\"bits\":\"",
      room.min_x * 16,
      room.min_y * 8,
      width,
      height);

   for (i = 0; i < total; i += 4)
   {
      int n;
      int value = 0;
      for (n = 0; n < 4 && i + n < total; n++)
      {
         int local = i + n;
         int x = room.min_x + local % width;
         int y = room.min_y + local / width;
         if (g_agent_room_seen[PALPAL_AgentRoomIndex(x, y)])
         {
            value |= 1 << n;
         }
      }
      fputc("0123456789abcdef"[value], fp);
   }

   fprintf(fp, "\"}");
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
      fprintf(fp, "{\"slot\":%d,", i);
      PALPAL_AgentWriteItemMetadata(fp, gpGlobals->rgInventory[i].wItem);
      fprintf(fp,
         ",\"amount\":%d,"
         "\"amount_in_use\":%d,"
         "\"equipped_count\":%d,"
         "\"total_count\":%d}",
         gpGlobals->rgInventory[i].nAmount,
         gpGlobals->rgInventory[i].nAmountInUse,
         PALPAL_AgentEquippedCount(gpGlobals->rgInventory[i].wItem),
         PAL_CountItem(gpGlobals->rgInventory[i].wItem));
      first = 0;
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteShop(
   FILE *fp
)
{
   int i;

   fprintf(fp, "\"shop\":");
   if (!g_agent_shop.active || gpGlobals == NULL ||
      (g_agent_shop.mode == kPalpalAgentShopModeBuy &&
       g_agent_shop.store_id >= gpGlobals->g.nStore))
   {
      fputs("null", fp);
      return;
   }

   fprintf(fp,
      "{\"active\":true,\"mode\":\"%s\",\"store_id\":",
      g_agent_shop.mode == kPalpalAgentShopModeSell ? "sell" : "buy");
   if (g_agent_shop.mode == kPalpalAgentShopModeSell)
   {
      fputs("null", fp);
   }
   else
   {
      fprintf(fp, "%u", (unsigned int)g_agent_shop.store_id);
   }
   fprintf(fp,
      ",\"phase\":\"%s\",\"selected_index\":%d,\"selected_item_id\":%u,"
      "\"items\":[",
      g_agent_shop.phase == kPalpalAgentShopPhaseConfirm ? "confirm" : "select",
      g_agent_shop.selected_index,
      (unsigned int)g_agent_shop.selected_item_id);
   if (g_agent_shop.mode == kPalpalAgentShopModeSell)
   {
      for (i = 0; i < g_agent_shop.item_count; i++)
      {
         WORD item_id = gpGlobals->rgInventory[i].wItem;
         int amount = gpGlobals->rgInventory[i].nAmount;
         int amount_in_use = gpGlobals->rgInventory[i].nAmountInUse;
         WORD flags = gpGlobals->g.rgObject[item_id].item.wFlags;
         WORD sell_price = gpGlobals->g.rgObject[item_id].item.wPrice / 2;
         if (i > 0)
         {
            fprintf(fp, ",");
         }
         fprintf(fp, "{\"index\":%d,", i);
         PALPAL_AgentWriteItemMetadata(fp, item_id);
         fprintf(fp,
            ",\"amount\":%d,\"amount_in_use\":%d,\"available_count\":%d,"
            "\"sell_price\":%u,\"can_sell\":%s,\"selected\":%s}",
            amount,
            amount_in_use,
            amount - amount_in_use,
            (unsigned int)sell_price,
            (flags & kItemFlagSellable) && amount > amount_in_use ? "true" : "false",
            i == g_agent_shop.selected_index ? "true" : "false");
      }
      fprintf(fp, "]}");
      return;
   }
   for (i = 0; i < g_agent_shop.item_count; i++)
   {
      WORD item_id = gpGlobals->g.lprgStore[g_agent_shop.store_id].rgwItems[i];
      int inventory_count = PAL_GetItemAmount(item_id);
      int equipped_count = PALPAL_AgentEquippedCount(item_id);
      WORD price = gpGlobals->g.rgObject[item_id].item.wPrice;
      if (i > 0)
      {
         fprintf(fp, ",");
      }
      fprintf(fp, "{\"index\":%d,", i);
      PALPAL_AgentWriteItemMetadata(fp, item_id);
      fprintf(fp,
         ",\"inventory_count\":%d,\"equipped_count\":%d,\"owned_count\":%d,"
         "\"affordable\":%s,\"selected\":%s}",
         inventory_count,
         equipped_count,
         inventory_count + equipped_count,
         price <= gpGlobals->dwCash ? "true" : "false",
         i == g_agent_shop.selected_index ? "true" : "false");
   }
   fprintf(fp, "]}");
}

static VOID
PALPAL_AgentWriteEquipment(
   FILE *fp,
   WORD role
)
{
   int i;

   fprintf(fp, "\"equipment\":[");
   for (i = 0; i < MAX_PLAYER_EQUIPMENTS; i++)
   {
      WORD item_id = gpGlobals->g.PlayerRoles.rgwEquipment[i][role];
      if (i > 0) fprintf(fp, ",");
      fprintf(fp, "{\"slot_id\":%d,\"slot_key\":\"%s\",", i, PALPAL_AgentEquipmentSlotKey(i));
      PALPAL_AgentWriteItemMetadata(fp, item_id);
      fprintf(fp, "}");
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
         "{\"index\":%d,\"role_id\":%u,\"scene_sprite_id\":%u,\"name\":",
         i,
         role,
         gpGlobals->g.PlayerRoles.rgwSpriteNum[role]);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(gpGlobals->g.PlayerRoles.rgwName[role]));
      fprintf(fp,
         ",\"level\":%u,\"hp\":%u,\"max_hp\":%u,"
         "\"mp\":%u,\"max_mp\":%u,"
         "\"attack_strength\":%u,"
         "\"magic_strength\":%u,"
         "\"defense\":%u,"
         "\"dexterity\":%u,"
         "\"flee_rate\":%u,"
         "\"poison_resistance\":%u,"
         "\"elemental_resistance\":[%u,%u,%u,%u,%u],"
         "\"x\":%d,\"y\":%d,\"frame\":%u,",
         gpGlobals->g.PlayerRoles.rgwLevel[role],
         gpGlobals->g.PlayerRoles.rgwHP[role],
         gpGlobals->g.PlayerRoles.rgwMaxHP[role],
         gpGlobals->g.PlayerRoles.rgwMP[role],
         gpGlobals->g.PlayerRoles.rgwMaxMP[role],
         PAL_GetPlayerAttackStrength(role),
         PAL_GetPlayerMagicStrength(role),
         PAL_GetPlayerDefense(role),
         PAL_GetPlayerDexterity(role),
         PAL_GetPlayerFleeRate(role),
         PAL_GetPlayerPoisonResistance(role),
         PAL_GetPlayerElementalResistance(role, 0),
         PAL_GetPlayerElementalResistance(role, 1),
         PAL_GetPlayerElementalResistance(role, 2),
         PAL_GetPlayerElementalResistance(role, 3),
         PAL_GetPlayerElementalResistance(role, 4),
         gpGlobals->rgParty[i].x,
         gpGlobals->rgParty[i].y,
         gpGlobals->rgParty[i].wFrame);
      PALPAL_AgentWriteEquipment(fp, role);
      fprintf(fp, ",\"status_effects\":");
      PALPAL_AgentWriteStatusEffects(fp, gpGlobals->rgPlayerStatus[role]);
      fprintf(fp, ",\"poisons\":");
      PALPAL_AgentWritePlayerPoisons(fp, i);
      fprintf(fp, "}");
   }
   fprintf(fp, "]");
}

static const char *
PALPAL_AgentBattleResultName(
   int result
)
{
   switch (result)
   {
   case kBattleResultWon: return "won";
   case kBattleResultLost: return "lost";
   case kBattleResultFleed: return "fled";
   case kBattleResultTerminated: return "terminated";
   case kBattleResultPause: return "paused";
   case kBattleResultPreBattle: return "pre_battle";
   default: return "ongoing";
   }
}

static const char *
PALPAL_AgentBattlePhaseName(
   VOID
)
{
   switch (g_agent_battle.phase)
   {
   case kPalpalAgentBattlePreBattle:
      return "pre_battle";
   case kPalpalAgentBattleCommand: return "command";
   case kPalpalAgentBattleReward: return "reward";
   case kPalpalAgentBattleEnding: return "ending";
   default: return "resolving";
   }
}

static const char *
PALPAL_AgentFighterStateName(
   FIGHTERSTATE state
)
{
   switch (state)
   {
   case kFighterCom: return "command";
   case kFighterAct: return "acting";
   default: return "waiting";
   }
}

static const char *
PALPAL_AgentBattleActionName(
   BATTLEACTIONTYPE action
)
{
   switch (action)
   {
   case kBattleActionDefend: return "defend";
   case kBattleActionAttack: return "attack";
   case kBattleActionMagic: return "magic";
   case kBattleActionCoopMagic: return "coop_magic";
   case kBattleActionFlee: return "flee";
   case kBattleActionThrowItem: return "throw_item";
   case kBattleActionUseItem: return "use_item";
   case kBattleActionAttackMate: return "attack_mate";
   default: return "pass";
   }
}

static VOID
PALPAL_AgentWriteStatusValues(
   FILE *fp,
   WORD *values
)
{
   int i;
   fprintf(fp, "[");
   for (i = 0; i < kStatusAll; i++)
   {
      if (i > 0) fprintf(fp, ",");
      fprintf(fp, "%u", values[i]);
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteStatusEffects(
   FILE *fp,
   WORD *values
)
{
   int i, first = 1;
   fprintf(fp, "[");
   for (i = 0; i < kStatusAll; i++)
   {
      WORD value = values[i];
      if (value == 0) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp,
         "{\"status_id\":%d,\"key\":\"%s\",\"remaining_rounds\":",
         i, PALPAL_AgentStatusKey((WORD)i));
      if (value > 999)
      {
         fprintf(fp, "null,\"persistent\":true}");
      }
      else
      {
         fprintf(fp, "%u,\"persistent\":false}", value);
      }
      first = 0;
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWritePlayerPoisons(
   FILE *fp,
   WORD party_index
)
{
   int i, first = 1;
   fprintf(fp, "[");
   for (i = 0; i < MAX_POISONS; i++)
   {
      POISONSTATUS *poison = &gpGlobals->rgPoisonStatus[i][party_index];
      if (poison->wPoisonID == 0) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"poison_id\":%u,\"script\":%u,\"name\":",
         poison->wPoisonID, poison->wPoisonScript);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(poison->wPoisonID));
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteEnemyPoisons(
   FILE *fp,
   BATTLEENEMY *enemy
)
{
   int i, first = 1;
   fprintf(fp, "[");
   for (i = 0; i < MAX_POISONS; i++)
   {
      POISONSTATUS *poison = &enemy->rgPoisons[i];
      if (poison->wPoisonID == 0) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"poison_id\":%u,\"script\":%u,\"name\":",
         poison->wPoisonID, poison->wPoisonScript);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(poison->wPoisonID));
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "]");
}

static BOOL
PALPAL_AgentPoisonListsDiffer(
   WORD *before,
   WORD *after
)
{
   int i;
   for (i = 0; i < MAX_POISONS; i++)
      if (before[i] != after[i]) return TRUE;
   return FALSE;
}

static VOID
PALPAL_AgentWriteVisiblePoisonIds(
   FILE *fp,
   WORD *values
)
{
   int i, first = 1;
   fprintf(fp, "[");
   for (i = 0; i < MAX_POISONS; i++)
   {
      if (values[i] == 0) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"poison_id\":%u,\"name\":", values[i]);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(values[i]));
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteVisibleOutcome(
   FILE *fp,
   PALPALAGENTVISIBLEOUTCOME *outcome
)
{
   int i, first, numeric = outcome->number_count > 0;
   if (outcome->round_end)
      fprintf(fp, "{\"sequence\":%u,\"actor\":{\"side\":\"environment\"",
         (unsigned int)outcome->sequence);
   else
      fprintf(fp, "{\"sequence\":%u,\"actor\":{\"side\":\"%s\",\"index\":%u",
      (unsigned int)outcome->sequence,
      outcome->player_actor ? "player" : "enemy", outcome->actor_index);
   if (outcome->player_actor)
   {
      fprintf(fp, ",\"role_id\":%u,\"name\":", outcome->actor_role);
      PALPAL_AgentWriteJsonWideString(fp,
         PAL_GetWord(gpGlobals->g.PlayerRoles.rgwName[outcome->actor_role]));
   }
   else if (!outcome->round_end)
   {
      WORD enemy_object_id = outcome->actor_index < MAX_ENEMIES_IN_TEAM
         ? outcome->enemy_object_id[outcome->actor_index] : 0;
      fprintf(fp, ",\"object_id\":%u,\"name\":", enemy_object_id);
      PALPAL_AgentWriteJsonWideString(fp, enemy_object_id ? PAL_GetWord(enemy_object_id) : L"");
   }
   fprintf(fp, "},\"action\":{\"action_type\":\"%s\"",
      outcome->round_end ? "round_end" : outcome->player_actor
         ? PALPAL_AgentBattleActionName((BATTLEACTIONTYPE)outcome->action_type)
         : "enemy_action");
   if (outcome->player_actor && outcome->object_id)
   {
      fprintf(fp, ",\"object_id\":%u,\"name\":", outcome->object_id);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(outcome->object_id));
   }
   if (outcome->player_actor && outcome->mp_cost > 0)
      fprintf(fp, ",\"mp_cost\":%d", outcome->mp_cost);
   fprintf(fp, "},\"target\":{\"index\":%d", outcome->target_index);
   if (outcome->target_object_id)
   {
      fprintf(fp, ",\"side\":\"enemy\",\"object_id\":%u,\"name\":",
         outcome->target_object_id);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(outcome->target_object_id));
   }
   else if (outcome->target_index == -1)
      fprintf(fp, ",\"mode\":\"all\"");
   fprintf(fp, "},\"player_changes\":[");
   first = 1;
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      BOOL status_changed = memcmp(outcome->player_status_before[i],
         outcome->player_status_after[i], sizeof(outcome->player_status_before[i])) != 0;
      BOOL poison_changed = PALPAL_AgentPoisonListsDiffer(
         outcome->player_poison_before[i], outcome->player_poison_after[i]);
      if (outcome->player_hp_before[i] == outcome->player_hp_after[i] &&
          outcome->player_mp_before[i] == outcome->player_mp_after[i] &&
          !status_changed && !poison_changed) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"index\":%d,\"hp\":{\"before\":%u,\"after\":%u,\"max\":%u},"
         "\"mp\":{\"before\":%u,\"after\":%u,\"max\":%u},\"knocked_out\":%s,"
         "\"status_before\":",
         i, outcome->player_hp_before[i], outcome->player_hp_after[i],
         outcome->player_hp_max[i], outcome->player_mp_before[i],
         outcome->player_mp_after[i], outcome->player_mp_max[i],
         outcome->player_hp_after[i] == 0 ? "true" : "false");
      PALPAL_AgentWriteStatusEffects(fp, outcome->player_status_before[i]);
      fprintf(fp, ",\"status_after\":");
      PALPAL_AgentWriteStatusEffects(fp, outcome->player_status_after[i]);
      fprintf(fp, ",\"poisons_before\":");
      PALPAL_AgentWriteVisiblePoisonIds(fp, outcome->player_poison_before[i]);
      fprintf(fp, ",\"poisons_after\":");
      PALPAL_AgentWriteVisiblePoisonIds(fp, outcome->player_poison_after[i]);
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "],\"enemy_changes\":[");
   first = 1;
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      int delta;
      if (!outcome->enemy_object_id[i] ||
          outcome->enemy_hp_before[i] == outcome->enemy_hp_after[i]) continue;
      delta = (SHORT)(outcome->enemy_hp_after[i] - outcome->enemy_hp_before[i]);
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"slot_index\":%d,\"object_id\":%u,\"name\":",
         i, outcome->enemy_object_id[i]);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(outcome->enemy_object_id[i]));
      fprintf(fp, ",\"amount\":%d,\"kind\":\"%s\"",
         delta < 0 ? -delta : delta, delta < 0 ? "damage" : "recovery");
      /* Match PAL_BattlePostActionCheck: lethal overkill may wrap the WORD HP,
       * but the game evaluates it as signed before clearing the enemy slot. */
      if (outcome->enemy_hp_before[i] > 0 && (SHORT)outcome->enemy_hp_after[i] <= 0)
         fprintf(fp, ",\"defeated\":true");
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "],\"numbers\":[");
   for (i = 0; i < outcome->number_count; i++)
   {
      SHORT change = outcome->numbers[i].change;
      WORD index = outcome->numbers[i].target_index;
      if (i > 0) fprintf(fp, ",");
      fprintf(fp, "{\"side\":\"%s\",\"target_index\":%u,\"stat\":\"%s\","
         "\"amount\":%d,\"kind\":\"%s\"",
         outcome->numbers[i].enemy ? "enemy" : "player", index,
         outcome->numbers[i].mp ? "mp" : "hp",
         change < 0 ? -change : change, change < 0 ? "damage" : "recovery");
      if (outcome->numbers[i].enemy && index < MAX_ENEMIES_IN_TEAM &&
          outcome->enemy_object_id[index])
      {
         fprintf(fp, ",\"object_id\":%u,\"name\":", outcome->enemy_object_id[index]);
         PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(outcome->enemy_object_id[index]));
      }
      else if (!outcome->numbers[i].enemy && index <= gpGlobals->wMaxPartyMemberIndex)
      {
         WORD role = gpGlobals->rgParty[index].wPlayerRole;
         fprintf(fp, ",\"role_id\":%u,\"name\":", role);
         PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(gpGlobals->g.PlayerRoles.rgwName[role]));
      }
      fprintf(fp, "}");
   }
   fprintf(fp, "],\"battle_messages\":[");
   for (i = 0; i < outcome->message_count; i++)
   {
      if (i) fprintf(fp, ",");
      PALPAL_AgentWriteJsonWideString(fp, outcome->messages[i]);
   }
   fprintf(fp, "],\"battle_messages_truncated\":%s,\"enemy_targets\":[",
      outcome->messages_truncated ? "true" : "false");
   first = 1;
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      if (!outcome->enemy_target || !outcome->enemy_object_id[i] ||
          (outcome->target_index != -1 && outcome->target_index != i)) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"slot_index\":%d,\"object_id\":%u,\"name\":",
         i, outcome->enemy_object_id[i]);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(outcome->enemy_object_id[i]));
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "],\"no_numeric_change_observed\":%s}", numeric ? "false" : "true");
}

static VOID
PALPAL_AgentWriteVisibleOutcomes(
   FILE *fp,
   PALPALAGENTVISIBLEOUTCOME *outcomes,
   int count
)
{
   int i;
   fprintf(fp, "\"visible_outcomes\":[");
   for (i = 0; i < count; i++)
   {
      if (i > 0) fprintf(fp, ",");
      PALPAL_AgentWriteVisibleOutcome(fp, &outcomes[i]);
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteBattleTargets(
   FILE *fp,
   BOOL enemy_side
)
{
   int i, first = 1;
   fprintf(fp, "[");
   if (enemy_side)
   {
      for (i = 0; i <= g_Battle.wMaxEnemyIndex; i++)
      {
         if (g_Battle.rgEnemy[i].wObjectID == 0 || g_Battle.rgEnemy[i].e.wHealth == 0)
            continue;
         if (!first) fprintf(fp, ",");
         fprintf(fp, "%d", i);
         first = 0;
      }
   }
   else
   {
      for (i = 0; i <= gpGlobals->wMaxPartyMemberIndex; i++)
      {
         if (!first) fprintf(fp, ",");
         fprintf(fp, "%d", i);
         first = 0;
      }
   }
   fprintf(fp, "]");
}

static BOOL
PALPAL_AgentAppendObjectDescription(
   WCHAR *output,
   size_t *length,
   LPCWSTR text,
   BOOL new_line
)
{
   size_t text_length;

   if (text == NULL || text[0] == L'\0') return TRUE;
   text_length = wcslen(text);
   if (*length >= PALPAL_AGENT_OBJECT_DESC_CHARS ||
       (new_line && *length > 0 && *length == PALPAL_AGENT_OBJECT_DESC_CHARS - 1) ||
       text_length > PALPAL_AGENT_OBJECT_DESC_CHARS - 1 - *length -
          ((new_line && *length > 0) ? 1 : 0))
      return FALSE;
   if (new_line && *length > 0) output[(*length)++] = L'\n';
   memcpy(output + *length, text, text_length * sizeof(WCHAR));
   *length += text_length;
   output[*length] = L'\0';
   return TRUE;
}

static BOOL
PALPAL_AgentReadWin95ObjectDescription(
   WORD script,
   WCHAR *output
)
{
   BOOL first_line = TRUE;
   BOOL next_new_line = TRUE;
   size_t length = 0;
   int steps = 0;

   while (script != 0)
   {
      LPSCRIPTENTRY entry;
      if (script >= gpGlobals->g.nScriptEntry || ++steps > gpGlobals->g.nScriptEntry)
         return FALSE;
      entry = &gpGlobals->g.lprgScriptEntry[script];
      if (entry->wOperation == 0) break;
      if (entry->wOperation == 0x00A7)
      {
         script++;
         continue;
      }
      if (entry->wOperation != 0xFFFF) return FALSE;

      if (gConfig.pszMsgFile)
      {
         WORD start = script;
         WORD current = script;
         WORD first_message = entry->rgwOperand[0];
         WORD last_message = first_message;
         int order = 0, message;

         while (current < gpGlobals->g.nScriptEntry)
         {
            LPSCRIPTENTRY candidate = &gpGlobals->g.lprgScriptEntry[current];
            if (candidate->wOperation == 0x008E)
            {
               current++;
               continue;
            }
            if (candidate->wOperation != 0xFFFF ||
                (current != start && candidate->rgwOperand[0] != (WORD)(last_message + 1)))
               break;
            last_message = candidate->rgwOperand[0];
            current++;
         }
         if (current == start || current > gpGlobals->g.nScriptEntry) return FALSE;
         while ((message = PAL_GetMsgNum(first_message, last_message - first_message, order++)) >= 0)
         {
            if (message > 0)
            {
               if (!PALPAL_AgentAppendObjectDescription(output, &length,
                     PAL_GetMsg(message), !first_line)) return FALSE;
               if (PAL_GetMsg(message)[0] != L'\0') first_line = FALSE;
            }
         }
         script = current;
      }
      else
      {
         LPCWSTR message = PAL_GetMsg(entry->rgwOperand[0]);
         if (!PALPAL_AgentAppendObjectDescription(output, &length, message,
               !first_line && next_new_line))
            return FALSE;
         if (message[0] != L'\0') first_line = FALSE;
         next_new_line = entry->rgwOperand[1] != 1;
         script++;
      }
   }
   return length > 0;
}

static LPCWSTR
PALPAL_AgentObjectDescription(
   WORD object_id,
   BOOL item
)
{
   PALPALAGENTOBJECTDESC *cached;

   if (object_id == 0 || object_id >= MAX_OBJECTS) return NULL;
   cached = item ? &g_agent_item_desc[object_id] : &g_agent_magic_desc[object_id];
   if (!cached->resolved)
   {
      cached->resolved = TRUE;
      cached->text[0] = L'\0';
      if (gConfig.fIsWIN95)
      {
         WORD script = item ? gpGlobals->g.rgObject[object_id].item.wScriptDesc :
            gpGlobals->g.rgObject[object_id].magic.wScriptDesc;
         if (script == 0 || !PALPAL_AgentReadWin95ObjectDescription(script, cached->text))
            cached->text[0] = L'\0';
      }
      else
      {
         LPCWSTR source = PAL_GetObjectDesc(gpGlobals->lpObjectDesc, object_id);
         size_t i, length;
         if (source != NULL && (length = wcslen(source)) < PALPAL_AGENT_OBJECT_DESC_CHARS)
         {
            BOOL has_text = FALSE;
            for (i = 0; i < length; i++)
            {
               cached->text[i] = source[i] == L'*' ? L'\n' : source[i];
               if (cached->text[i] != L'\n' && cached->text[i] != L'\r' &&
                   cached->text[i] != L' ' && cached->text[i] != L'\t') has_text = TRUE;
            }
            cached->text[length] = L'\0';
            if (!has_text) cached->text[0] = L'\0';
         }
      }
   }
   return cached->text[0] == L'\0' ? NULL : cached->text;
}

static LPCWSTR
PALPAL_AgentMagicDescription(
   WORD object_id
)
{
   return PALPAL_AgentObjectDescription(object_id, FALSE);
}

static LPCWSTR
PALPAL_AgentItemDescription(
   WORD object_id
)
{
   return PALPAL_AgentObjectDescription(object_id, TRUE);
}

static VOID
PALPAL_AgentWriteBattleActionEntry(
   FILE *fp,
   BOOL *first,
   const char *type,
   WORD object_id,
   const char *target_side,
   BOOL target_all,
   int mp_cost,
   LPCWSTR description
)
{
   if (!*first) fprintf(fp, ",");
   fprintf(fp,
      "{\"action_type\":\"%s\",\"object_id\":%u,\"target_side\":\"%s\","
      "\"target_mode\":\"%s\",\"valid_target_indices\":",
      type, object_id, target_side, target_all ? "all" :
      (strcmp(target_side, "none") == 0 ? "none" : "single"));
   if (target_all || strcmp(target_side, "none") == 0)
   {
      fprintf(fp, "[]");
   }
   else
   {
      PALPAL_AgentWriteBattleTargets(fp, strcmp(target_side, "enemy") == 0);
   }
   fprintf(fp, ",\"submission_target_indices\":");
   if (target_all)
   {
      fprintf(fp, "[-1]");
   }
   else if (strcmp(target_side, "none") == 0)
   {
      fprintf(fp, "[-2]");
   }
   else
   {
      PALPAL_AgentWriteBattleTargets(fp, strcmp(target_side, "enemy") == 0);
   }
   if (object_id != 0)
   {
      fprintf(fp, ",\"name\":");
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(object_id));
   }
   if (mp_cost >= 0)
   {
      fprintf(fp, ",\"mp_cost\":%d", mp_cost);
   }
   if (description != NULL && description[0] != L'\0')
   {
      fprintf(fp, ",\"description\":");
      PALPAL_AgentWriteJsonWideString(fp, description);
   }
   fprintf(fp, "}");
   *first = FALSE;
}

static VOID
PALPAL_AgentWriteBattleActions(
   FILE *fp
)
{
   BOOL first = TRUE;
   WORD player_index = g_agent_battle.current_player_index;
   WORD role;
   int i;

   fprintf(fp, "\"available_actions\":[");
   if (g_agent_battle.phase == kPalpalAgentBattleReward && g_agent_battle.awaiting_ack)
   {
      PALPAL_AgentWriteBattleActionEntry(fp, &first, "continue", 0, "none", FALSE, -1, NULL);
   }
   else if (g_agent_battle.phase == kPalpalAgentBattleCommand &&
            player_index <= gpGlobals->wMaxPartyMemberIndex)
   {
      role = gpGlobals->rgParty[player_index].wPlayerRole;
      PALPAL_AgentWriteBattleActionEntry(fp, &first, "attack", 0, "enemy",
         PAL_PlayerCanAttackAll(role), -1, NULL);
      if (gpGlobals->rgPlayerStatus[role][kStatusSilence] == 0)
      {
         for (i = 0; i < MAX_PLAYER_MAGICS; i++)
         {
            WORD object_id = gpGlobals->g.PlayerRoles.rgwMagic[i][role];
            WORD number, flags;
            if (object_id == 0 || object_id >= MAX_OBJECTS) continue;
            number = gpGlobals->g.rgObject[object_id].magic.wMagicNumber;
            flags = gpGlobals->g.rgObject[object_id].magic.wFlags;
            if (number >= gpGlobals->g.nMagic ||
                !(flags & kMagicFlagUsableInBattle) ||
                gpGlobals->g.lprgMagic[number].wCostMP > gpGlobals->g.PlayerRoles.rgwMP[role])
               continue;
            PALPAL_AgentWriteBattleActionEntry(fp, &first, "magic", object_id,
               (flags & kMagicFlagUsableToEnemy) ? "enemy" : "player",
               (flags & kMagicFlagApplyToAll) != 0,
               gpGlobals->g.lprgMagic[number].wCostMP,
               PALPAL_AgentMagicDescription(object_id));
         }
      }
      if (PALPAL_BattleCoopActionAvailable())
      {
         WORD object_id = PAL_GetPlayerCooperativeMagic(role);
         WORD flags = gpGlobals->g.rgObject[object_id].magic.wFlags;
         if (object_id != 0)
         {
            PALPAL_AgentWriteBattleActionEntry(fp, &first, "coop_magic", object_id,
               (flags & kMagicFlagUsableToEnemy) ? "enemy" : "player",
               (flags & kMagicFlagApplyToAll) != 0, -1,
               PALPAL_AgentMagicDescription(object_id));
         }
      }
      for (i = 0; i < MAX_INVENTORY; i++)
      {
         WORD object_id = gpGlobals->rgInventory[i].wItem;
         WORD flags;
         if (object_id == 0 || gpGlobals->rgInventory[i].nAmount <=
               gpGlobals->rgInventory[i].nAmountInUse) continue;
         flags = gpGlobals->g.rgObject[object_id].item.wFlags;
         if (flags & kItemFlagUsable)
            PALPAL_AgentWriteBattleActionEntry(fp, &first, "use_item", object_id,
               "player", (flags & kItemFlagApplyToAll) != 0, -1,
               PALPAL_AgentItemDescription(object_id));
         if (flags & kItemFlagThrowable)
            PALPAL_AgentWriteBattleActionEntry(fp, &first, "throw_item", object_id,
               "enemy", (flags & kItemFlagApplyToAll) != 0, -1,
               PALPAL_AgentItemDescription(object_id));
      }
      PALPAL_AgentWriteBattleActionEntry(fp, &first, "defend", 0, "none", FALSE, -1, NULL);
      if (!g_Battle.fIsBoss)
         PALPAL_AgentWriteBattleActionEntry(fp, &first, "flee", 0, "none", FALSE, -1, NULL);
   }
   fprintf(fp, "]");
}

static VOID
PALPAL_AgentWriteBattle(
   FILE *fp
)
{
   int i;

   fprintf(fp, "\"battle\":");
   if (!gpGlobals->fInBattle)
   {
      fprintf(fp, "null");
      return;
   }
   for (i = 0; i <= g_Battle.wMaxEnemyIndex; i++)
   {
      WORD object_id = g_Battle.rgEnemy[i].wObjectID;
      if (object_id != 0 && g_agent_battle.enemy_object_id[i] != object_id)
      {
         g_agent_battle.enemy_object_id[i] = object_id;
         g_agent_battle.enemy_max_hp[i] = g_Battle.rgEnemy[i].e.wHealth;
      }
   }
   fprintf(fp,
      "{\"generation\":%u,\"command_generation\":%u,\"result\":\"%s\","
      "\"phase\":\"%s\",\"is_boss\":%s,\"current_player_index\":%u,"
      "\"awaiting_ack\":%s,\"players\":[",
      (unsigned int)g_agent_battle.generation,
      (unsigned int)g_agent_battle.command_generation,
      PALPAL_AgentBattleResultName(g_Battle.BattleResult),
      PALPAL_AgentBattlePhaseName(),
      g_Battle.fIsBoss ? "true" : "false",
      g_agent_battle.current_player_index,
      g_agent_battle.awaiting_ack ? "true" : "false");
   for (i = 0; i <= gpGlobals->wMaxPartyMemberIndex; i++)
   {
      WORD role = gpGlobals->rgParty[i].wPlayerRole;
      BATTLEPLAYER *player = &g_Battle.rgPlayer[i];
      if (i > 0) fprintf(fp, ",");
      fprintf(fp, "{\"index\":%d,\"role_id\":%u,\"name\":", i, role);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(gpGlobals->g.PlayerRoles.rgwName[role]));
      fprintf(fp,
         ",\"hp\":%u,\"max_hp\":%u,\"mp\":%u,\"max_mp\":%u,\"level\":%u,"
         "\"attack_strength\":%u,\"magic_strength\":%u,\"defense\":%u,"
         "\"dexterity\":%u,\"flee_rate\":%u,\"poison_resistance\":%u,"
         "\"attack_all\":%s,\"cooperative_magic_id\":%u,"
         "\"elemental_resistance\":[%u,%u,%u,%u,%u],\"status\":",
         gpGlobals->g.PlayerRoles.rgwHP[role],
         gpGlobals->g.PlayerRoles.rgwMaxHP[role],
         gpGlobals->g.PlayerRoles.rgwMP[role],
         gpGlobals->g.PlayerRoles.rgwMaxMP[role],
         gpGlobals->g.PlayerRoles.rgwLevel[role],
         PAL_GetPlayerAttackStrength(role), PAL_GetPlayerMagicStrength(role),
         PAL_GetPlayerDefense(role), PAL_GetPlayerDexterity(role),
         PAL_GetPlayerFleeRate(role), PAL_GetPlayerPoisonResistance(role),
         PAL_PlayerCanAttackAll(role) ? "true" : "false",
         PAL_GetPlayerCooperativeMagic(role),
         PAL_GetPlayerElementalResistance(role, 0),
         PAL_GetPlayerElementalResistance(role, 1),
         PAL_GetPlayerElementalResistance(role, 2),
         PAL_GetPlayerElementalResistance(role, 3),
         PAL_GetPlayerElementalResistance(role, 4));
      PALPAL_AgentWriteStatusValues(fp, gpGlobals->rgPlayerStatus[role]);
      fprintf(fp, ",\"status_effects\":");
      PALPAL_AgentWriteStatusEffects(fp, gpGlobals->rgPlayerStatus[role]);
      fprintf(fp, ",\"poisons\":");
      PALPAL_AgentWritePlayerPoisons(fp, i);
      /* Action fields survive between rounds. Only kFighterAct is committed;
       * an incapacitated role cannot receive a fresh command even if recovered
       * later during this round. Never label the retained old action pending. */
      fprintf(fp, ",\"command_status\":\"%s\"",
         gpGlobals->g.PlayerRoles.rgwHP[role] == 0 ||
         gpGlobals->rgPlayerStatus[role][kStatusSleep] ||
         gpGlobals->rgPlayerStatus[role][kStatusConfused] ||
         gpGlobals->rgPlayerStatus[role][kStatusParalyzed] ? "unable" :
         player->state == kFighterAct ? "committed" :
         player->state == kFighterCom ? "selecting" : "unselected");
      fprintf(fp,
         ",\"time_meter\":%.2f,\"fighter_state\":\"%s\",\"defending\":%s,"
         "\"submitted_action\":{\"action_type\":\"%s\",\"object_id\":%u,\"target_index\":%d}}",
         player->flTimeMeter, PALPAL_AgentFighterStateName(player->state),
         player->fDefending ? "true" : "false",
         PALPAL_AgentBattleActionName(player->action.ActionType),
         player->action.wActionID, player->action.sTarget);
   }
   fprintf(fp, "],\"enemies\":[");
   for (i = 0; i <= g_Battle.wMaxEnemyIndex; i++)
   {
      BATTLEENEMY *enemy = &g_Battle.rgEnemy[i];
      WORD object_id = enemy->wObjectID;
      if (i > 0) fprintf(fp, ",");
      fprintf(fp,
         "{\"slot_index\":%d,\"present\":%s,\"object_id\":%u,\"enemy_id\":%u,\"name\":",
         i, object_id ? "true" : "false", object_id,
         object_id ? gpGlobals->g.rgObject[object_id].enemy.wEnemyID : 0);
      PALPAL_AgentWriteJsonWideString(fp, object_id ? PAL_GetWord(object_id) : L"");
      fprintf(fp,
         ",\"hp\":%u,\"max_hp\":%u,\"level\":%u,\"attack_strength\":%u,"
         "\"magic_strength\":%u,\"defense\":%u,\"dexterity\":%u,\"flee_rate\":%u,"
         "\"poison_resistance\":%u,\"physical_resistance\":%u,"
         "\"exp\":%u,\"cash\":%u,\"magic_id\":%u,\"magic_rate\":%u,"
         "\"attack_equiv_item_id\":%u,\"attack_equiv_item_rate\":%u,"
         "\"steal_item_id\":%u,\"steal_item_count\":%u,\"dual_move\":%u,"
         "\"collect_value\":%u,"
         "\"elemental_resistance\":[%u,%u,%u,%u,%u],\"status\":",
         enemy->e.wHealth, g_agent_battle.enemy_max_hp[i], enemy->e.wLevel,
         enemy->e.wAttackStrength, enemy->e.wMagicStrength, enemy->e.wDefense,
         enemy->e.wDexterity, enemy->e.wFleeRate, enemy->e.wPoisonResistance,
         enemy->e.wPhysicalResistance, enemy->e.wExp, enemy->e.wCash,
         enemy->e.wMagic, enemy->e.wMagicRate, enemy->e.wAttackEquivItem,
         enemy->e.wAttackEquivItemRate, enemy->e.wStealItem, enemy->e.nStealItem,
         enemy->e.wDualMove, enemy->e.wCollectValue, enemy->e.wElemResistance[0],
         enemy->e.wElemResistance[1], enemy->e.wElemResistance[2],
         enemy->e.wElemResistance[3], enemy->e.wElemResistance[4]);
      PALPAL_AgentWriteStatusValues(fp, enemy->rgwStatus);
      fprintf(fp, ",\"poisons\":");
      PALPAL_AgentWriteEnemyPoisons(fp, enemy);
      fprintf(fp, ",\"time_meter\":%.2f,\"fighter_state\":\"%s\"}",
         enemy->flTimeMeter, PALPAL_AgentFighterStateName(enemy->state));
   }
   fprintf(fp, "],");
   PALPAL_AgentWriteBattleActions(fp);
   fprintf(fp, ",");
   fprintf(fp, "\"hp_tracking_safe\":%s,", g_agent_battle.hp_tracking_safe ? "true" : "false");
   PALPAL_AgentWriteVisibleOutcomes(fp, g_agent_visible_outcomes,
      g_agent_visible_outcome_count);
   fprintf(fp, "}");
}

static VOID
PALPAL_AgentWriteLastBattle(
   FILE *fp
)
{
   int i, first;
   fprintf(fp, "\"last_battle\":");
   if (!g_agent_last_battle.valid)
   {
      fprintf(fp, "null");
      return;
   }
   fprintf(fp,
      "{\"generation\":%u,\"result\":\"%s\",\"is_boss\":%s,\"ended_frame\":%u,"
      "\"duration_ms\":%u,\"exp_gained\":%d,\"cash_gained\":%d,\"enemies\":[",
      (unsigned int)g_agent_last_battle.generation,
      PALPAL_AgentBattleResultName(g_agent_last_battle.result),
      g_agent_last_battle.is_boss ? "true" : "false",
      (unsigned int)g_agent_last_battle.ended_frame,
      (unsigned int)g_agent_last_battle.duration_ms,
      g_agent_last_battle.exp_gained, g_agent_last_battle.cash_gained);
   first = 1;
   for (i = 0; i < MAX_ENEMIES_IN_TEAM; i++)
   {
      WORD object_id = g_agent_last_battle.enemy_object_id[i];
      if (!object_id) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp, "{\"object_id\":%u,\"name\":", object_id);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(object_id));
      fprintf(fp, "}");
      first = 0;
   }
   fprintf(fp, "],\"players\":[");
   first = 1;
   for (i = 0; i < MAX_PLAYERS_IN_PARTY; i++)
   {
      WORD role = g_agent_last_battle.player_role[i];
      if (role == 0xFFFF || role >= MAX_PLAYER_ROLES) continue;
      if (!first) fprintf(fp, ",");
      fprintf(fp,
         "{\"index\":%d,\"role_id\":%u,\"name\":", i, role);
      PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(gpGlobals->g.PlayerRoles.rgwName[role]));
      fprintf(fp,
         ",\"hp_before\":%u,\"hp_after\":%u,\"mp_before\":%u,\"mp_after\":%u,"
         "\"level_before\":%u,\"level_after\":%u,\"knocked_out\":%s,"
         "\"status_before\":",
         g_agent_last_battle.player_hp_before[i], g_agent_last_battle.player_hp_after[i],
         g_agent_last_battle.player_mp_before[i], g_agent_last_battle.player_mp_after[i],
         g_agent_last_battle.player_level_before[i], g_agent_last_battle.player_level_after[i],
         g_agent_last_battle.player_hp_after[i] == 0 ? "true" : "false");
      PALPAL_AgentWriteStatusValues(fp, g_agent_last_battle.player_status_before[i]);
      fprintf(fp, ",\"status_after\":");
      PALPAL_AgentWriteStatusValues(fp, g_agent_last_battle.player_status_after[i]);
      fprintf(fp, ",\"new_magics\":[");
      {
         int j, magic_first = 1;
         for (j = 0; j < MAX_PLAYER_MAGICS; j++)
         {
            WORD magic = g_agent_last_battle.player_new_magics[i][j];
            if (magic == 0) continue;
            if (!magic_first) fprintf(fp, ",");
            fprintf(fp, "{\"object_id\":%u,\"name\":", magic);
            PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(magic));
            fprintf(fp, "}");
            magic_first = 0;
         }
      }
      fprintf(fp, "]}");
      first = 0;
   }
   fprintf(fp, "],");
   fprintf(fp, "\"hp_tracking_safe\":%s,", g_agent_last_battle.hp_tracking_safe ? "true" : "false");
   PALPAL_AgentWriteVisibleOutcomes(fp, g_agent_last_battle.visible_outcomes,
      g_agent_last_battle.visible_outcome_count);
   fprintf(fp, "}");
}

static const char *
PALPAL_AgentDialogPositionName(
   BYTE position
)
{
   switch (position)
   {
   case kDialogUpper:
      return "upper";
   case kDialogCenter:
      return "center";
   case kDialogLower:
      return "lower";
   case kDialogCenterWindow:
      return "center_window";
   default:
      return "unknown";
   }
}

static BOOL
PALPAL_AgentIsSpace(
   WCHAR ch
)
{
   return ch == L' ' || ch == L'\t' || ch == 0x3000;
}

static BOOL
PALPAL_AgentIsSpeakerLine(
   LPCWSTR text
)
{
   size_t len;
   WCHAR last;

   if (text == NULL)
   {
      return FALSE;
   }

   len = wcslen(text);
   while (len > 0 && PALPAL_AgentIsSpace(text[len - 1]))
   {
      len--;
   }
   if (len < 2 || len > 32)
   {
      return FALSE;
   }

   last = text[len - 1];
   return last == L':' || last == 0xff1a || last == 0x2236;
}

static VOID
PALPAL_AgentCopySpeakerName(
   WCHAR *dst,
   size_t dst_len,
   LPCWSTR text
)
{
   size_t len;

   if (dst_len == 0)
   {
      return;
   }

   dst[0] = 0;
   if (text == NULL)
   {
      return;
   }

   len = wcslen(text);
   while (len > 0 && PALPAL_AgentIsSpace(text[len - 1]))
   {
      len--;
   }
   if (len > 0 && (text[len - 1] == L':' || text[len - 1] == 0xff1a || text[len - 1] == 0x2236))
   {
      len--;
   }
   while (len > 0 && PALPAL_AgentIsSpace(text[len - 1]))
   {
      len--;
   }
   if (len >= dst_len)
   {
      len = dst_len - 1;
   }
   wcsncpy(dst, text, len);
   dst[len] = 0;
}

VOID
PALPAL_AgentObserveDialogStart(
   WORD wEventObjectID
)
{
   (void)wEventObjectID;

   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   PALPAL_AgentClearDialogSpeaker();
}

VOID
PALPAL_AgentObserveDialogText(
   INT iMessageID,
   WORD wEventObjectID,
   LPCWSTR lpszText
)
{
   PALPALDIALOGEVENT *event;
   PALPALDIALOGEVENT *slot;

   if (!PALPAL_AgentEnabled() || gpGlobals == NULL || lpszText == NULL || lpszText[0] == 0)
   {
      return;
   }

   /* Keep player-visible script feedback with the action that produced it.
    * Text applies to the whole action, not independently to every area target. */
   if (gpGlobals->fInBattle && g_agent_visible_active.valid)
   {
      int index = g_agent_visible_active.message_count;
      if (index < PALPAL_AGENT_VISIBLE_MESSAGE_CAPACITY)
      {
         PALPAL_AgentCopyWideString(g_agent_visible_active.messages[index], 256, lpszText);
         g_agent_visible_active.message_count++;
         if (wcslen(lpszText) >= 256)
            g_agent_visible_active.messages_truncated = TRUE;
      }
      else
         g_agent_visible_active.messages_truncated = TRUE;
   }

   if (g_TextLib.nCurrentDialogLine == 0 && PALPAL_AgentIsSpeakerLine(lpszText))
   {
      PALPAL_AgentCopySpeakerName(g_agent_dialog_speaker, sizeof(g_agent_dialog_speaker) / sizeof(g_agent_dialog_speaker[0]), lpszText);
      g_agent_dialog_speaker_bound = g_agent_dialog_speaker[0] != 0;
      g_agent_dialog_speaker_scene_id = gpGlobals->wNumScene;
      g_agent_dialog_speaker_map_id = PALPAL_AgentCurrentMapId();
      g_agent_dialog_speaker_event_object_id = wEventObjectID;
      return;
   }

   event = &g_agent_latest_dialog_event;
   memset(event, 0, sizeof(*event));
   event->valid = TRUE;
   event->sequence = ++g_agent_dialog_sequence;
   event->trigger_generation = g_agent_trigger_observation.generation;
   event->frame = gpGlobals->dwFrameNum;
   event->scene_id = gpGlobals->wNumScene;
   event->map_id = PALPAL_AgentCurrentMapId();
   event->event_object_id = wEventObjectID;
   event->message_id = iMessageID;
   event->position = g_TextLib.bDialogPosition;
   if (g_agent_dialog_speaker_bound &&
       g_agent_dialog_speaker_scene_id == event->scene_id &&
       g_agent_dialog_speaker_map_id == event->map_id &&
       g_agent_dialog_speaker_event_object_id == event->event_object_id)
   {
      PALPAL_AgentCopyWideString(event->speaker, sizeof(event->speaker) / sizeof(event->speaker[0]), g_agent_dialog_speaker);
   }
   PALPAL_AgentCopyWideString(event->text, sizeof(event->text) / sizeof(event->text[0]), lpszText);

   slot = &g_agent_dialog_events[g_agent_dialog_event_next];
   *slot = *event;
   g_agent_dialog_event_next = (g_agent_dialog_event_next + 1) % PALPAL_AGENT_DIALOG_EVENT_CAPACITY;
   if (g_agent_dialog_event_count < PALPAL_AGENT_DIALOG_EVENT_CAPACITY)
   {
      g_agent_dialog_event_count++;
   }
}

static VOID
PALPAL_AgentWriteDialogEvent(
   FILE *fp,
   const PALPALDIALOGEVENT *event
)
{
   fprintf(fp,
      "{\"sequence\":%u,"
      "\"trigger_generation\":%u,"
      "\"frame\":%u,"
      "\"scene_id\":%u,"
      "\"map_id\":%u,"
      "\"event_object_id\":%u,"
      "\"message_id\":%d,"
      "\"dialog_position\":\"%s\","
      "\"dialog_position_id\":%u,"
      "\"speaker_hint\":",
      (unsigned int)event->sequence,
      (unsigned int)event->trigger_generation,
      (unsigned int)event->frame,
      (unsigned int)event->scene_id,
      (unsigned int)event->map_id,
      (unsigned int)event->event_object_id,
      event->message_id,
      PALPAL_AgentDialogPositionName(event->position),
      (unsigned int)event->position);
   if (event->speaker[0])
   {
      PALPAL_AgentWriteJsonWideString(fp, event->speaker);
   }
   else
   {
      fputs("null", fp);
   }
   fputs(",\"text\":", fp);
   PALPAL_AgentWriteJsonWideString(fp, event->text);
   fputc('}', fp);
}

static VOID
PALPAL_AgentWriteDialog(
   FILE *fp
)
{
   DWORD now = SDL_GetTicks();
   BOOL visible = PAL_IsInDialog();
   BOOL active;
   DWORD inactive_ms = 0;

   if (visible)
   {
      g_agent_last_dialog_ticks = now;
   }

   active = visible ||
      (g_agent_last_dialog_ticks != 0 && now - g_agent_last_dialog_ticks <= PALPAL_DIALOG_ACTIVE_GRACE_MS);
   if (!visible && g_agent_last_dialog_ticks != 0)
   {
      inactive_ms = now - g_agent_last_dialog_ticks;
   }

   fprintf(fp,
      "\"dialog\":{\"active\":%s,"
      "\"visible\":%s,"
      "\"held\":%s,"
      "\"inactive_ms\":%u,"
      "\"position\":\"%s\","
      "\"position_id\":%u,"
      "\"current_line\":%d,"
      "\"font_color\":%u,"
      "\"icon\":%u,"
      "\"delay_time\":%d,"
      "\"user_skip\":%s,"
      "\"playing_rng\":%s,"
      "\"title_position\":{\"x\":%d,\"y\":%d},"
      "\"text_position\":{\"x\":%d,\"y\":%d},"
      "\"icon_position\":{\"x\":%d,\"y\":%d},"
      "\"latest_event\":",
      active ? "true" : "false",
      visible ? "true" : "false",
      (active && !visible) ? "true" : "false",
      (unsigned int)inactive_ms,
      PALPAL_AgentDialogPositionName(g_TextLib.bDialogPosition),
      (unsigned int)g_TextLib.bDialogPosition,
      g_TextLib.nCurrentDialogLine,
      (unsigned int)g_TextLib.bCurrentFontColor,
      (unsigned int)g_TextLib.bIcon,
      g_TextLib.iDelayTime,
      g_TextLib.fUserSkip ? "true" : "false",
      PAL_DialogIsPlayingRNG() ? "true" : "false",
      PALPAL_PosX(g_TextLib.posDialogTitle),
      PALPAL_PosY(g_TextLib.posDialogTitle),
      PALPAL_PosX(g_TextLib.posDialogText),
      PALPAL_PosY(g_TextLib.posDialogText),
      PALPAL_PosX(g_TextLib.posIcon),
      PALPAL_PosY(g_TextLib.posIcon));
   if (g_agent_latest_dialog_event.valid)
   {
      PALPAL_AgentWriteDialogEvent(fp, &g_agent_latest_dialog_event);
   }
   else
   {
      fputs("null", fp);
   }
   fputs(",\"events\":[", fp);
   if (g_agent_dialog_event_count > 0)
   {
      int i;
      int start = g_agent_dialog_event_count == PALPAL_AGENT_DIALOG_EVENT_CAPACITY ? g_agent_dialog_event_next : 0;
      for (i = 0; i < g_agent_dialog_event_count; i++)
      {
         const PALPALDIALOGEVENT *event = &g_agent_dialog_events[(start + i) % PALPAL_AGENT_DIALOG_EVENT_CAPACITY];
         if (!event->valid)
         {
            continue;
         }
         if (i > 0)
         {
            fputc(',', fp);
         }
         PALPAL_AgentWriteDialogEvent(fp, event);
      }
   }
   fputs("]}", fp);
}

static VOID
PALPAL_AgentWriteChoice(
   FILE *fp
)
{
   fprintf(fp, "\"choice\":");
   if (!g_agent_choice.active)
   {
      fputs("null", fp);
      return;
   }
   fprintf(fp,
      "{\"active\":true,\"generation\":%u,\"kind\":\"script_confirm\","
      "\"event_object_id\":%u,\"script_entry\":%u,"
      "\"selected_index\":%d,\"command_pending\":%s,\"prompt\":",
      (unsigned int)g_agent_choice.generation,
      (unsigned int)g_agent_choice.event_object_id,
      (unsigned int)g_agent_choice.script_entry,
      g_agent_choice.selected_index,
      g_agent_choice.command_pending ? "true" : "false");
   if (g_agent_latest_dialog_event.valid)
   {
      PALPAL_AgentWriteDialogEvent(fp, &g_agent_latest_dialog_event);
   }
   else
   {
      fputs("null", fp);
   }
   fputs(",\"options\":[{\"index\":0,\"semantic\":\"no\",\"word_id\":", fp);
   fprintf(fp, "%u,\"text\":", (unsigned int)CONFIRMMENU_LABEL_NO);
   PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(CONFIRMMENU_LABEL_NO));
   fputs("},{\"index\":1,\"semantic\":\"yes\",\"word_id\":", fp);
   fprintf(fp, "%u,\"text\":", (unsigned int)CONFIRMMENU_LABEL_YES);
   PALPAL_AgentWriteJsonWideString(fp, PAL_GetWord(CONFIRMMENU_LABEL_YES));
   fputs("}]}", fp);
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
            "{\"id\":%d,\"x\":%d,\"y\":%d,\"sprite_id\":%u,"
            "\"sprite_frames\":%u,\"current_frame\":%u,\"direction\":%u,"
            "\"layer\":%d,\"state\":%d,"
            "\"trigger_mode\":%u,\"trigger_script\":%u,\"auto_script\":%u,"
            "\"blocking\":%s,\"interactable\":%s,\"vanish_time\":%d}",
            i + 1,
            obj->x,
            obj->y,
            obj->wSpriteNum,
            obj->nSpriteFrames,
            obj->wCurrentFrameNum,
            obj->wDirection,
            obj->sLayer,
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

static VOID
PALPAL_WriteLE16(
   BYTE *p,
   WORD value
)
{
   p[0] = (BYTE)(value & 0xFF);
   p[1] = (BYTE)((value >> 8) & 0xFF);
}

static VOID
PALPAL_WriteLE32(
   BYTE *p,
   DWORD value
)
{
   p[0] = (BYTE)(value & 0xFF);
   p[1] = (BYTE)((value >> 8) & 0xFF);
   p[2] = (BYTE)((value >> 16) & 0xFF);
   p[3] = (BYTE)((value >> 24) & 0xFF);
}

static VOID
PALPAL_WriteLE64(
   BYTE *p,
   unsigned long long value
)
{
   int i;
   for (i = 0; i < 8; i++)
   {
      p[i] = (BYTE)((value >> (i * 8)) & 0xFF);
   }
}

enum {
   PALPAL_LIVE_HEADER_SIZE = 32,
   PALPAL_LIVE_VERSION = 1,
   PALPAL_LIVE_MESSAGE_HELLO = 1,
   PALPAL_LIVE_MESSAGE_STATE = 2,
   PALPAL_LIVE_MESSAGE_FRAME = 3,
   PALPAL_LIVE_MESSAGE_FRAME_REPEAT = 5,
   PALPAL_LIVE_RETRY_MS = 500,
   PALPAL_FRAME_WIDTH = 320,
   PALPAL_FRAME_HEIGHT = 200,
   PALPAL_FRAME_HEADER_SIZE = 24,
   PALPAL_FRAME_PALETTE_COLORS = 256,
   PALPAL_FRAME_PALETTE_SIZE = PALPAL_FRAME_PALETTE_COLORS * 4,
   PALPAL_FRAME_PIXEL_SIZE = PALPAL_FRAME_WIDTH * PALPAL_FRAME_HEIGHT,
   PALPAL_FRAME_PAYLOAD_SIZE = PALPAL_FRAME_PALETTE_SIZE + PALPAL_FRAME_PIXEL_SIZE,
   PALPAL_FRAME_MESSAGE_SIZE = PALPAL_FRAME_HEADER_SIZE + PALPAL_FRAME_PAYLOAD_SIZE,
   PALPAL_FRAME_FORMAT_INDEXED8 = 2,
   PALPAL_FRAME_INTERVAL_MS = 66
};

typedef struct tagPALPALLIVEMESSAGE
{
   BYTE *data;
   size_t length;
   size_t offset;
   WORD type;
} PALPALLIVEMESSAGE;

static int g_agent_live_socket = -1;
static int g_agent_live_connecting = 0;
static DWORD g_agent_live_retry_at = 0;
static unsigned long long g_agent_live_sequence = 0;
static BOOL g_agent_live_need_hello = TRUE;
static BOOL g_agent_live_force_frame = TRUE;
static PALPALLIVEMESSAGE *g_agent_live_current = NULL;
static PALPALLIVEMESSAGE *g_agent_live_state = NULL;
static PALPALLIVEMESSAGE *g_agent_live_frame = NULL;
static WORD g_agent_live_last_data_type = 0;
static BYTE g_agent_latest_frame[PALPAL_FRAME_PAYLOAD_SIZE];
static BYTE g_agent_published_frame[PALPAL_FRAME_PAYLOAD_SIZE];
static DWORD g_agent_latest_frame_num = 0;
static DWORD g_agent_last_frame_publish_time = 0;
static BOOL g_agent_have_latest_frame = FALSE;
static BOOL g_agent_have_published_frame = FALSE;
static BOOL g_agent_latest_frame_dirty = FALSE;

/* Optional POBJ trailer follows the unchanged PFRM image payload. Its mask
 * contains only pixels that survived the actual scene drawing order. */
#define PALPAL_OBJECT_TRAILER_MAX (8 + 65536 + PALPAL_FRAME_PIXEL_SIZE * 2)
static WORD g_agent_object_mask[PALPAL_FRAME_PIXEL_SIZE];
static unsigned int g_agent_object_drawn[65536];
static BOOL g_agent_object_clipped[65536];
static BOOL g_agent_object_drawing = FALSE;
static WORD g_agent_drawing_object = 0;
static BYTE g_agent_scene_pixels[PALPAL_FRAME_PIXEL_SIZE];
static BYTE g_agent_scene_trailer[PALPAL_OBJECT_TRAILER_MAX];
static size_t g_agent_scene_trailer_size = 0;
static BYTE g_agent_latest_trailer[PALPAL_OBJECT_TRAILER_MAX];
static size_t g_agent_latest_trailer_size = 0;
static BYTE g_agent_published_trailer[PALPAL_OBJECT_TRAILER_MAX];
static size_t g_agent_published_trailer_size = 0;

VOID PALPAL_AgentSceneBegin(VOID)
{
   g_agent_scene_trailer_size = 0;
   g_agent_object_drawing = PALPAL_AgentEnabled() && gpGlobals != NULL &&
      gpGlobals->fInMainGame && !gpGlobals->fInBattle &&
      !gpGlobals->fEnteringScene && !PAL_IsInDialog() &&
      !g_agent_shop.active && !g_agent_choice.active;
   if (g_agent_object_drawing)
   {
      memset(g_agent_object_mask, 0, sizeof(g_agent_object_mask));
      memset(g_agent_object_drawn, 0, sizeof(g_agent_object_drawn));
      memset(g_agent_object_clipped, 0, sizeof(g_agent_object_clipped));
   }
   g_agent_drawing_object = 0;
}

VOID PALPAL_AgentSceneObject(WORD object_id, BOOL clipped)
{
   g_agent_drawing_object = object_id;
   g_agent_object_clipped[object_id] = clipped;
}

VOID PALPAL_AgentDrawSpan(SDL_Surface *surface, int x, int y, int count)
{
   int i;
   if (!g_agent_object_drawing || surface != gpScreen ||
      y < 0 || y >= 200 || x < 0 || x + count > 320)
   {
      return;
   }
   g_agent_object_drawn[g_agent_drawing_object] += count;
   for (i = 0; i < count; i++)
   {
      g_agent_object_mask[y * 320 + x + i] = g_agent_drawing_object;
   }
}

VOID PALPAL_AgentSceneEnd(VOID)
{
   char *json = NULL;
   size_t length = 0;
   FILE *fp;
   int i, x, y, first = 1;
   if (!g_agent_object_drawing)
   {
      return;
   }
   g_agent_object_drawing = FALSE;
   fp = open_memstream(&json, &length);
   if (fp == NULL) return;
   PALPAL_AgentPartyPosition(&x, &y);
   fprintf(fp, "{\"version\":1,\"available\":true,\"game_instance_id\":\"%s\","
      "\"scene_id\":%u,\"map_id\":%u,\"party_position\":{\"x\":%d,\"y\":%d},",
      g_agent_game_instance_id, gpGlobals->wNumScene,
      PALPAL_AgentCurrentMapId(), x, y);
   PALPAL_AgentWriteRoom(fp, gpGlobals->wNumScene, PALPAL_AgentCurrentMapId(), x, y);
   fprintf(fp, ",\"objects\":[");
   for (i = 1; i <= gpGlobals->g.nEventObject && i < 65536; i++)
   {
      LPEVENTOBJECT obj;
      if (!g_agent_object_drawn[i]) continue;
      obj = &gpGlobals->g.lprgEventObject[i - 1];
      fprintf(fp, "%s{\"id\":%d,\"sprite_id\":%u,\"drawn_pixels\":%u,\"screen_clipped\":%s}",
         first ? "" : ",", i, obj->wSpriteNum, g_agent_object_drawn[i],
         g_agent_object_clipped[i] ? "true" : "false");
      first = 0;
   }
   fprintf(fp, "]}");
   fclose(fp);
   if (length <= 65536)
   {
      memcpy(g_agent_scene_trailer, "POBJ", 4);
      PALPAL_WriteLE32(g_agent_scene_trailer + 4, (DWORD)length);
      memcpy(g_agent_scene_trailer + 8, json, length);
      for (i = 0; i < PALPAL_FRAME_PIXEL_SIZE; i++)
      {
         PALPAL_WriteLE16(g_agent_scene_trailer + 8 + length + i * 2,
            g_agent_object_mask[i]);
      }
      g_agent_scene_trailer_size = 8 + length + PALPAL_FRAME_PIXEL_SIZE * 2;
      for (y = 0; y < 200; y++)
      {
         memcpy(g_agent_scene_pixels + y * 320,
            (BYTE *)gpScreen->pixels + y * gpScreen->pitch, 320);
      }
   }
   free(json);
}

static VOID
PALPAL_AgentFreeLiveMessage(
   PALPALLIVEMESSAGE **message
)
{
   if (message != NULL && *message != NULL)
   {
      free((*message)->data);
      free(*message);
      *message = NULL;
   }
}

static PALPALLIVEMESSAGE *
PALPAL_AgentBuildLiveMessage(
   WORD type,
   const VOID *payload,
   size_t payload_length
)
{
   PALPALLIVEMESSAGE *message;
   BYTE *header;

   if (payload_length > 2 * 1024 * 1024)
   {
      return NULL;
   }
   message = (PALPALLIVEMESSAGE *)calloc(1, sizeof(*message));
   if (message == NULL)
   {
      return NULL;
   }
   message->length = PALPAL_LIVE_HEADER_SIZE + payload_length;
   message->data = (BYTE *)malloc(message->length);
   if (message->data == NULL)
   {
      free(message);
      return NULL;
   }
   message->type = type;
   header = message->data;
   memset(header, 0, PALPAL_LIVE_HEADER_SIZE);
   memcpy(header, "PLIV", 4);
   PALPAL_WriteLE16(header + 4, PALPAL_LIVE_VERSION);
   PALPAL_WriteLE16(header + 6, type);
   PALPAL_WriteLE32(header + 8, 0);
   PALPAL_WriteLE32(header + 12, (DWORD)payload_length);
   PALPAL_WriteLE64(header + 16, 0);
   PALPAL_WriteLE64(header + 24, 0);
   if (payload_length > 0)
   {
      memcpy(message->data + PALPAL_LIVE_HEADER_SIZE, payload, payload_length);
   }
   return message;
}

static VOID
PALPAL_AgentSequenceLiveMessage(
   PALPALLIVEMESSAGE *message
)
{
   if (message == NULL)
   {
      return;
   }
   message->offset = 0;
   PALPAL_WriteLE64(message->data + 16, ++g_agent_live_sequence);
   PALPAL_WriteLE64(message->data + 24, (unsigned long long)SDL_GetTicks());
}

static VOID
PALPAL_AgentLiveDisconnected(
   VOID
)
{
   PALPALLIVEMESSAGE **slot = NULL;

   if (g_agent_live_socket >= 0)
   {
      close(g_agent_live_socket);
   }
   g_agent_live_socket = -1;
   g_agent_live_connecting = 0;
   g_agent_live_retry_at = SDL_GetTicks() + PALPAL_LIVE_RETRY_MS;
   g_agent_live_need_hello = TRUE;
   g_agent_live_force_frame = TRUE;
   g_agent_live_last_data_type = 0;

   if (g_agent_live_current != NULL)
   {
      if (g_agent_live_current->type == PALPAL_LIVE_MESSAGE_STATE)
      {
         slot = &g_agent_live_state;
      }
      else if (g_agent_live_current->type == PALPAL_LIVE_MESSAGE_FRAME)
      {
         slot = &g_agent_live_frame;
      }
      if (slot != NULL && *slot == NULL)
      {
         g_agent_live_current->offset = 0;
         *slot = g_agent_live_current;
         g_agent_live_current = NULL;
      }
   }
   PALPAL_AgentFreeLiveMessage(&g_agent_live_current);
   if (g_agent_live_frame != NULL &&
       g_agent_live_frame->type == PALPAL_LIVE_MESSAGE_FRAME_REPEAT)
   {
      PALPAL_AgentFreeLiveMessage(&g_agent_live_frame);
   }
}

static BOOL
PALPAL_AgentLiveConnect(
   VOID
)
{
   struct sockaddr_un address;
   int flags;

   if (g_agent_live_socket >= 0)
   {
      return TRUE;
   }
   if (g_agent_socket_path[0] == '\0' ||
       strlen(g_agent_socket_path) >= sizeof(address.sun_path) ||
       !SDL_TICKS_PASSED(SDL_GetTicks(), g_agent_live_retry_at))
   {
      return FALSE;
   }

   g_agent_live_socket = socket(AF_UNIX, SOCK_STREAM, 0);
   if (g_agent_live_socket < 0)
   {
      g_agent_live_retry_at = SDL_GetTicks() + PALPAL_LIVE_RETRY_MS;
      return FALSE;
   }
   flags = fcntl(g_agent_live_socket, F_GETFL, 0);
   if (flags < 0 || fcntl(g_agent_live_socket, F_SETFL, flags | O_NONBLOCK) < 0)
   {
      PALPAL_AgentLiveDisconnected();
      return FALSE;
   }
#ifdef SO_NOSIGPIPE
   {
      int enabled = 1;
      setsockopt(g_agent_live_socket, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
   }
#endif
   memset(&address, 0, sizeof(address));
   address.sun_family = AF_UNIX;
   strncpy(address.sun_path, g_agent_socket_path, sizeof(address.sun_path) - 1);
   if (connect(g_agent_live_socket, (struct sockaddr *)&address, sizeof(address)) == 0)
   {
      g_agent_live_connecting = 0;
      g_agent_live_need_hello = TRUE;
      return TRUE;
   }
   if (errno == EINPROGRESS)
   {
      g_agent_live_connecting = 1;
      return FALSE;
   }
   PALPAL_AgentLiveDisconnected();
   return FALSE;
}

static BOOL
PALPAL_AgentLiveFinishConnect(
   VOID
)
{
   fd_set writable;
   struct timeval timeout;
   int error = 0;
   socklen_t error_size = sizeof(error);

   if (!g_agent_live_connecting)
   {
      return g_agent_live_socket >= 0 ? TRUE : FALSE;
   }
   FD_ZERO(&writable);
   FD_SET(g_agent_live_socket, &writable);
   timeout.tv_sec = 0;
   timeout.tv_usec = 0;
   if (select(g_agent_live_socket + 1, NULL, &writable, NULL, &timeout) <= 0)
   {
      return FALSE;
   }
   if (getsockopt(g_agent_live_socket, SOL_SOCKET, SO_ERROR, &error, &error_size) < 0 || error != 0)
   {
      PALPAL_AgentLiveDisconnected();
      return FALSE;
   }
   g_agent_live_connecting = 0;
   g_agent_live_need_hello = TRUE;
   return TRUE;
}

static VOID
PALPAL_AgentPrepareLiveCurrent(
   VOID
)
{
   char hello[160];
   int hello_length;

   if (g_agent_live_current != NULL)
   {
      return;
   }
   if (g_agent_live_need_hello)
   {
      hello_length = snprintf(hello, sizeof(hello),
         "{\"game_instance_id\":\"%s\",\"frame_format\":\"indexed8\"}",
         g_agent_game_instance_id);
      if (hello_length > 0 && (size_t)hello_length < sizeof(hello))
      {
         g_agent_live_current = PALPAL_AgentBuildLiveMessage(
            PALPAL_LIVE_MESSAGE_HELLO, hello, (size_t)hello_length);
         PALPAL_AgentSequenceLiveMessage(g_agent_live_current);
      }
      g_agent_live_need_hello = FALSE;
      return;
   }
   if (g_agent_live_state != NULL && g_agent_live_frame != NULL)
   {
      if (g_agent_live_last_data_type != PALPAL_LIVE_MESSAGE_FRAME)
      {
         g_agent_live_current = g_agent_live_frame;
         g_agent_live_frame = NULL;
      }
      else
      {
         g_agent_live_current = g_agent_live_state;
         g_agent_live_state = NULL;
      }
   }
   else if (g_agent_live_state != NULL)
   {
      g_agent_live_current = g_agent_live_state;
      g_agent_live_state = NULL;
   }
   else if (g_agent_live_frame != NULL)
   {
      g_agent_live_current = g_agent_live_frame;
      g_agent_live_frame = NULL;
   }
   if (g_agent_live_current != NULL)
   {
      g_agent_live_last_data_type =
         g_agent_live_current->type == PALPAL_LIVE_MESSAGE_FRAME_REPEAT
            ? PALPAL_LIVE_MESSAGE_FRAME
            : g_agent_live_current->type;
   }
   PALPAL_AgentSequenceLiveMessage(g_agent_live_current);
}

static VOID
PALPAL_AgentPumpLive(
   VOID
)
{
   int message_count = 0;

   if (!PALPAL_AgentLiveConnect() && g_agent_live_socket < 0)
   {
      return;
   }
   if (!PALPAL_AgentLiveFinishConnect())
   {
      return;
   }

   while (message_count < 3)
   {
      ssize_t sent;
      int send_flags = 0;
      PALPAL_AgentPrepareLiveCurrent();
      if (g_agent_live_current == NULL)
      {
         return;
      }
#ifdef MSG_NOSIGNAL
      send_flags = MSG_NOSIGNAL;
#endif
      sent = send(
         g_agent_live_socket,
         g_agent_live_current->data + g_agent_live_current->offset,
         g_agent_live_current->length - g_agent_live_current->offset,
         send_flags);
      if (sent > 0)
      {
         g_agent_live_current->offset += (size_t)sent;
         if (g_agent_live_current->offset == g_agent_live_current->length)
         {
            PALPAL_AgentFreeLiveMessage(&g_agent_live_current);
            message_count++;
            continue;
         }
         continue;
      }
      if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
      {
         return;
      }
      PALPAL_AgentLiveDisconnected();
      return;
   }
}

static BOOL
PALPAL_AgentQueueLiveMessage(
   WORD type,
   const VOID *payload,
   size_t payload_length
)
{
   PALPALLIVEMESSAGE *message;
   PALPALLIVEMESSAGE **slot;

   PALPAL_AgentPumpLive();
   message = PALPAL_AgentBuildLiveMessage(type, payload, payload_length);
   if (message == NULL)
   {
      return FALSE;
   }
   slot = type == PALPAL_LIVE_MESSAGE_FRAME || type == PALPAL_LIVE_MESSAGE_FRAME_REPEAT
      ? &g_agent_live_frame
      : &g_agent_live_state;
   if (type == PALPAL_LIVE_MESSAGE_FRAME_REPEAT &&
       *slot != NULL &&
       (*slot)->type == PALPAL_LIVE_MESSAGE_FRAME)
   {
      PALPAL_AgentFreeLiveMessage(&message);
      PALPAL_AgentPumpLive();
      return TRUE;
   }
   PALPAL_AgentFreeLiveMessage(slot);
   *slot = message;
   PALPAL_AgentPumpLive();
   return TRUE;
}

VOID
PALPAL_AgentWriteFrame(
   VOID
)
{
   SDL_Palette *palette;
   int i, y;

   if (!PALPAL_AgentEnabled() || gpScreen == NULL)
   {
      return;
   }

#if SDL_VERSION_ATLEAST(3,0,0)
   palette = SDL_GetSurfacePalette(gpScreen);
#else
   palette = gpScreen->format ? gpScreen->format->palette : NULL;
#endif
   if (palette == NULL || palette->colors == NULL)
   {
      return;
   }

   if (SDL_MUSTLOCK(gpScreen) && SDL_LockSurface(gpScreen) < 0)
   {
      return;
   }

   for (i = 0; i < PALPAL_FRAME_PALETTE_COLORS; i++)
   {
      SDL_Color color = i < palette->ncolors ? palette->colors[i] : palette->colors[0];
      BYTE *entry = g_agent_latest_frame + i * 4;
      entry[0] = color.r;
      entry[1] = color.g;
      entry[2] = color.b;
      entry[3] = 0xFF;
   }
   for (y = 0; y < PALPAL_FRAME_HEIGHT; y++)
   {
      BYTE *src = (BYTE *)gpScreen->pixels + y * gpScreen->pitch;
      BYTE *dst = g_agent_latest_frame + PALPAL_FRAME_PALETTE_SIZE +
         y * PALPAL_FRAME_WIDTH;
      memcpy(dst, src, PALPAL_FRAME_WIDTH);
   }

   if (SDL_MUSTLOCK(gpScreen))
   {
      SDL_UnlockSurface(gpScreen);
   }

   if (gpGlobals != NULL)
   {
      g_agent_latest_frame_num = gpGlobals->dwFrameNum;
   }
   else
   {
      g_agent_latest_frame_num = 0;
   }
   g_agent_have_latest_frame = TRUE;
   g_agent_latest_trailer_size = 0;
   if (g_agent_scene_trailer_size && gpGlobals != NULL &&
       !gpGlobals->fInBattle && !gpGlobals->fEnteringScene &&
       !PAL_IsInDialog() && !g_agent_shop.active && !g_agent_choice.active &&
       memcmp(g_agent_scene_pixels,
          g_agent_latest_frame + PALPAL_FRAME_PALETTE_SIZE,
          PALPAL_FRAME_PIXEL_SIZE) == 0)
   {
      g_agent_latest_trailer_size = g_agent_scene_trailer_size;
      memcpy(g_agent_latest_trailer, g_agent_scene_trailer, g_agent_scene_trailer_size);
   }
   g_agent_latest_frame_dirty = TRUE;
}

VOID
PALPAL_AgentPollFrame(
   VOID
)
{
   static BYTE s_frame[PALPAL_FRAME_MESSAGE_SIZE + PALPAL_OBJECT_TRAILER_MAX];
   DWORD now;
   BOOL changed;

   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   PALPAL_AgentPumpLive();
   if (!g_agent_have_latest_frame)
   {
      return;
   }

   now = SDL_GetTicks();
   if (g_agent_last_frame_publish_time != 0 &&
       !SDL_TICKS_PASSED(now,
          g_agent_last_frame_publish_time + PALPAL_FRAME_INTERVAL_MS))
   {
      return;
   }

   changed = g_agent_latest_frame_dirty &&
      (!g_agent_have_published_frame ||
       g_agent_published_trailer_size != g_agent_latest_trailer_size ||
       memcmp(g_agent_published_trailer, g_agent_latest_trailer,
          g_agent_latest_trailer_size) != 0 ||
       memcmp(g_agent_published_frame, g_agent_latest_frame,
          PALPAL_FRAME_PAYLOAD_SIZE) != 0);
   if (!g_agent_live_force_frame && !changed)
   {
      if (PALPAL_AgentQueueLiveMessage(PALPAL_LIVE_MESSAGE_FRAME_REPEAT, NULL, 0))
      {
         g_agent_last_frame_publish_time = now;
         g_agent_latest_frame_dirty = FALSE;
      }
      return;
   }

   memset(s_frame, 0, PALPAL_FRAME_HEADER_SIZE);
   memcpy(s_frame, "PFRM", 4);
   PALPAL_WriteLE16(s_frame + 4, 2);
   PALPAL_WriteLE16(s_frame + 6, PALPAL_FRAME_WIDTH);
   PALPAL_WriteLE16(s_frame + 8, PALPAL_FRAME_HEIGHT);
   PALPAL_WriteLE16(s_frame + 10, PALPAL_FRAME_FORMAT_INDEXED8);
   PALPAL_WriteLE32(s_frame + 12, PALPAL_FRAME_PALETTE_COLORS);
   PALPAL_WriteLE32(s_frame + 16, g_agent_latest_frame_num);
   PALPAL_WriteLE32(s_frame + 20, PALPAL_FRAME_PAYLOAD_SIZE);
   memcpy(s_frame + PALPAL_FRAME_HEADER_SIZE, g_agent_latest_frame,
      PALPAL_FRAME_PAYLOAD_SIZE);
   memcpy(s_frame + PALPAL_FRAME_MESSAGE_SIZE, g_agent_latest_trailer,
      g_agent_latest_trailer_size);
   if (PALPAL_AgentQueueLiveMessage(PALPAL_LIVE_MESSAGE_FRAME, s_frame,
      PALPAL_FRAME_MESSAGE_SIZE + g_agent_latest_trailer_size))
   {
      g_agent_published_trailer_size = g_agent_latest_trailer_size;
      memcpy(g_agent_published_trailer, g_agent_latest_trailer, g_agent_latest_trailer_size);
      memcpy(g_agent_published_frame, g_agent_latest_frame,
         PALPAL_FRAME_PAYLOAD_SIZE);
      g_agent_have_published_frame = TRUE;
      g_agent_latest_frame_dirty = FALSE;
      g_agent_live_force_frame = FALSE;
      g_agent_last_frame_publish_time = now;
   }
}

static VOID
PALPAL_AgentWriteStartupState(
   VOID
)
{
   char *payload = NULL;
   size_t payload_length = 0;
   FILE *fp;

   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   fp = open_memstream(&payload, &payload_length);
   if (!fp)
   {
      return;
   }

   fprintf(fp, "{");
   fprintf(fp, "\"status\":\"startup_waiting\",");
   fprintf(fp, "\"game_instance_id\":\"%s\",", g_agent_game_instance_id);
   fprintf(fp, "\"frame\":0,");
   fprintf(fp, "\"in_main_game\":false,");
   fprintf(fp, "\"in_battle\":false,");
   fprintf(fp, "\"battle\":null,");
   fprintf(fp, "\"last_battle\":null,");
   fprintf(fp, "\"choice\":null,");
   fprintf(fp, "\"entering_scene\":false,");
   fprintf(fp, "\"startup_mode\":\"wait\",");
   fprintf(fp, "\"selected_save_slot\":%d", g_agent_startup_slot);
   fprintf(fp, "}\n");

   fclose(fp);
   PALPAL_AgentQueueLiveMessage(PALPAL_LIVE_MESSAGE_STATE, payload, payload_length);
   free(payload);
}

VOID
PALPAL_AgentWriteState(
   VOID
)
{
   char *payload = NULL;
   size_t payload_length = 0;
   FILE *fp;
   int scene_index;
   WORD map_id = 0;
   int player_x;
   int player_y;

   if (!PALPAL_AgentEnabled())
   {
      return;
   }
   if (!gpGlobals->fInMainGame)
   {
      if (g_agent_startup_waiting)
      {
         PALPAL_AgentWriteStartupState();
      }
      return;
   }

   scene_index = gpGlobals->wNumScene > 0 ? gpGlobals->wNumScene - 1 : 0;
   if (scene_index >= 0 && scene_index < MAX_SCENES)
   {
      map_id = gpGlobals->g.rgScene[scene_index].wMapNum;
   }

   player_x = PALPAL_PosX(gpGlobals->viewport) + PALPAL_PosX(gpGlobals->partyoffset);
   player_y = PALPAL_PosY(gpGlobals->viewport) + PALPAL_PosY(gpGlobals->partyoffset);

   fp = open_memstream(&payload, &payload_length);
   if (!fp)
   {
      return;
   }

   fprintf(fp, "{");
   fprintf(fp, "\"game_instance_id\":\"%s\",", g_agent_game_instance_id);
   fprintf(fp,
      "\"agent_action\":{\"generation\":%u,\"pending\":%s,"
      "\"remaining_steps\":%d,\"waiting_for_move\":%s},",
      (unsigned int)g_agent_action_generation,
      (g_agent_action_pending || g_agent_action_count > 0 || g_agent_waiting_for_move) ? "true" : "false",
      PALPAL_AgentRemainingActionSteps(),
      g_agent_waiting_for_move ? "true" : "false");
   fprintf(fp, "\"checkpoint\":{\"paused\":%s,\"load_failed\":%s},",
      g_agent_checkpoint_paused ? "true" : "false", g_agent_checkpoint_failed ? "true" : "false");
   fprintf(fp, "\"frame\":%u,", (unsigned int)gpGlobals->dwFrameNum);
   fprintf(fp, "\"in_main_game\":%s,", gpGlobals->fInMainGame ? "true" : "false");
   fprintf(fp, "\"in_battle\":%s,", gpGlobals->fInBattle ? "true" : "false");
   PALPAL_AgentWriteBattle(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteLastBattle(fp);
   fprintf(fp, ",");
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
   if (g_agent_trigger_observation.valid)
   {
      fprintf(fp,
         "\"trigger_observation\":{\"generation\":%u,\"event_object_id\":%u,"
         "\"trigger_script\":%u,\"completed\":%s,\"source\":{\"scene_id\":%u,"
         "\"map_id\":%u,\"x\":%d,\"y\":%d}",
         (unsigned int)g_agent_trigger_observation.generation,
         (unsigned int)g_agent_trigger_observation.event_object_id,
         (unsigned int)g_agent_trigger_observation.trigger_script,
         g_agent_trigger_observation.completed ? "true" : "false",
         (unsigned int)g_agent_trigger_observation.source_scene_id,
         (unsigned int)g_agent_trigger_observation.source_map_id,
         g_agent_trigger_observation.source_x,
         g_agent_trigger_observation.source_y);
      if (g_agent_trigger_observation.completed)
      {
         fprintf(fp,
            ",\"destination\":{\"scene_id\":%u,\"map_id\":%u,\"x\":%d,\"y\":%d,\"frame\":%u}",
            (unsigned int)g_agent_trigger_observation.destination_scene_id,
            (unsigned int)g_agent_trigger_observation.destination_map_id,
            g_agent_trigger_observation.destination_x,
            g_agent_trigger_observation.destination_y,
            (unsigned int)g_agent_trigger_observation.destination_frame);
      }
      fprintf(fp, "},");
   }
   PALPAL_AgentWriteRoom(fp, gpGlobals->wNumScene, map_id, player_x, player_y);
   fprintf(fp, ",");
   PALPAL_AgentWriteDialog(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteChoice(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteShop(fp);
   fprintf(fp, ",");

   PALPAL_AgentWriteParty(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteInventory(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteObjects(fp);
   fprintf(fp, ",");
   PALPAL_AgentWriteWalkability(fp, player_x, player_y);
   fprintf(fp, ",");
   PALPAL_AgentWriteNavGrid(fp, player_x, player_y);
   fprintf(fp, "}\n");

   fclose(fp);
   PALPAL_AgentQueueLiveMessage(PALPAL_LIVE_MESSAGE_STATE, payload, payload_length);
   free(payload);
}
