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
#include "text.h"

#include <errno.h>
#include <time.h>

static int g_agent_initialized = 0;
static int g_agent_enabled = 0;
static int g_agent_headless = 0;
static BOOL g_agent_startup_waiting = FALSE;
static BOOL g_agent_startup_ready = FALSE;
static int g_agent_startup_slot = 0;
static char g_agent_runtime_dir[PAL_MAX_PATH] = "runtime";
static char g_agent_game_instance_id[65];
static PALDIRECTION g_agent_injected_dir = kDirUnknown;
static BOOL g_agent_action_pending = FALSE;
static BOOL g_agent_dir_active = FALSE;
static DWORD g_agent_last_dialog_ticks = 0;
static DWORD g_agent_dialog_sequence = 0;
static WCHAR g_agent_dialog_speaker[64];

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

typedef struct tagPALPALACTIONSTEP
{
   DWORD buttons;
   PALDIRECTION dir;
} PALPALACTIONSTEP;

static PALPALACTIONSTEP g_agent_action_queue[PALPAL_AGENT_MAX_ACTION_STEPS];
static int g_agent_action_count = 0;
static int g_agent_action_index = 0;
static DWORD g_agent_last_action_ticks = 0;
static BOOL g_agent_waiting_for_move = FALSE;
static int g_agent_wait_x = 0;
static int g_agent_wait_y = 0;
static DWORD g_agent_wait_ticks = 0;

static VOID PALPAL_AgentClearActionQueue(VOID);

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

typedef struct tagPALPALDIALOGEVENT
{
   BOOL valid;
   DWORD sequence;
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

#define PALPAL_DIALOG_ACTIVE_GRACE_MS 1000

static VOID
PALPAL_AgentInit(
   VOID
)
{
   const char *enabled;
   const char *headless;
   const char *runtime_dir;
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
      "\"error\":",
      (unsigned int)SDL_GetTicks(),
      ok ? "true" : "false",
      status ? status : "unknown",
      action ? action : "unknown",
      slot);
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

static BOOL
PALPAL_AgentSlotCommandAllowed(
   const char *action,
   int slot
)
{
   if (slot < 0)
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
   if (gpGlobals->fEnteringScene)
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

   PAL_SaveGame(slot, PALPAL_AgentNextSavedTimes());
   PALPAL_AgentWriteSlotResult(TRUE, "saved", "save_slot", slot, NULL);
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
   BOOL pending_target_all = FALSE;
   WORD pending_item_id = 0;
   WORD pending_role_id = 0;
   WORD pending_equip_item_id = 0;
   WORD pending_equip_role_id = 0;
   PALPALAGENTSLOTCOMMAND pending_slot_command = kPalpalAgentSlotCommandNone;
   int pending_slot = 0;

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
   remove(path);
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
      if (step->dir != kDirUnknown && PALPAL_AgentPartyPosition(&x, &y))
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
      fprintf(fp, "}");
   }
   fprintf(fp, "]");
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

   g_agent_dialog_speaker[0] = 0;
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

   if (g_TextLib.nCurrentDialogLine == 0 && PALPAL_AgentIsSpeakerLine(lpszText))
   {
      PALPAL_AgentCopySpeakerName(g_agent_dialog_speaker, sizeof(g_agent_dialog_speaker) / sizeof(g_agent_dialog_speaker[0]), lpszText);
      return;
   }

   event = &g_agent_latest_dialog_event;
   memset(event, 0, sizeof(*event));
   event->valid = TRUE;
   event->sequence = ++g_agent_dialog_sequence;
   event->frame = gpGlobals->dwFrameNum;
   event->scene_id = gpGlobals->wNumScene;
   event->map_id = PALPAL_AgentCurrentMapId();
   event->event_object_id = wEventObjectID;
   event->message_id = iMessageID;
   event->position = g_TextLib.bDialogPosition;
   PALPAL_AgentCopyWideString(event->speaker, sizeof(event->speaker) / sizeof(event->speaker[0]), g_agent_dialog_speaker);
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
      "\"frame\":%u,"
      "\"scene_id\":%u,"
      "\"map_id\":%u,"
      "\"event_object_id\":%u,"
      "\"message_id\":%d,"
      "\"dialog_position\":\"%s\","
      "\"dialog_position_id\":%u,"
      "\"speaker_hint\":",
      (unsigned int)event->sequence,
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

VOID
PALPAL_AgentWriteFrame(
   VOID
)
{
   enum {
      PALPAL_FRAME_WIDTH = 320,
      PALPAL_FRAME_HEIGHT = 200,
      PALPAL_FRAME_HEADER_SIZE = 24,
      PALPAL_FRAME_PAYLOAD_SIZE = PALPAL_FRAME_WIDTH * PALPAL_FRAME_HEIGHT * 4,
      PALPAL_FRAME_FORMAT_RGBA8888 = 1,
      PALPAL_FRAME_INTERVAL_MS = 66
   };
   static DWORD s_last_frame_write_time = 0;
   static BYTE rgba[PALPAL_FRAME_PAYLOAD_SIZE];
   char frame_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   BYTE header[PALPAL_FRAME_HEADER_SIZE];
   SDL_Palette *palette;
   DWORD now;
   DWORD frame_num = 0;
   FILE *fp;
   int x, y;

   if (!PALPAL_AgentEnabled() || gpScreen == NULL)
   {
      return;
   }

   now = SDL_GetTicks();
   if (s_last_frame_write_time != 0 &&
       !SDL_TICKS_PASSED(now, s_last_frame_write_time + PALPAL_FRAME_INTERVAL_MS))
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

   for (y = 0; y < PALPAL_FRAME_HEIGHT; y++)
   {
      BYTE *src = (BYTE *)gpScreen->pixels + y * gpScreen->pitch;
      BYTE *dst = rgba + y * PALPAL_FRAME_WIDTH * 4;
      for (x = 0; x < PALPAL_FRAME_WIDTH; x++)
      {
         BYTE index = src[x];
         SDL_Color color = index < palette->ncolors ? palette->colors[index] : palette->colors[0];
         dst[x * 4 + 0] = color.r;
         dst[x * 4 + 1] = color.g;
         dst[x * 4 + 2] = color.b;
         dst[x * 4 + 3] = 0xFF;
      }
   }

   if (SDL_MUSTLOCK(gpScreen))
   {
      SDL_UnlockSurface(gpScreen);
   }

   if (gpGlobals != NULL)
   {
      frame_num = gpGlobals->dwFrameNum;
   }

   memset(header, 0, sizeof(header));
   header[0] = 'P';
   header[1] = 'F';
   header[2] = 'R';
   header[3] = 'M';
   PALPAL_WriteLE16(header + 4, 1);
   PALPAL_WriteLE16(header + 6, PALPAL_FRAME_WIDTH);
   PALPAL_WriteLE16(header + 8, PALPAL_FRAME_HEIGHT);
   PALPAL_WriteLE16(header + 10, PALPAL_FRAME_FORMAT_RGBA8888);
   PALPAL_WriteLE32(header + 12, 0);
   PALPAL_WriteLE32(header + 16, frame_num);
   PALPAL_WriteLE32(header + 20, PALPAL_FRAME_PAYLOAD_SIZE);

   PALPAL_AgentPath(frame_path, sizeof(frame_path), "frame.rgba");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "frame.rgba.tmp");
   fp = fopen(tmp_path, "wb");
   if (!fp)
   {
      return;
   }

   if (fwrite(header, 1, sizeof(header), fp) != sizeof(header) ||
       fwrite(rgba, 1, sizeof(rgba), fp) != sizeof(rgba))
   {
      fclose(fp);
      remove(tmp_path);
      return;
   }

   fclose(fp);
   rename(tmp_path, frame_path);
   s_last_frame_write_time = now;
}

static VOID
PALPAL_AgentWriteStartupState(
   VOID
)
{
   char state_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
   FILE *fp;

   if (!PALPAL_AgentEnabled())
   {
      return;
   }

   PALPAL_AgentPath(state_path, sizeof(state_path), "state.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "state.json.tmp");
   fp = fopen(tmp_path, "w");
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
   fprintf(fp, "\"entering_scene\":false,");
   fprintf(fp, "\"startup_mode\":\"wait\",");
   fprintf(fp, "\"selected_save_slot\":%d", g_agent_startup_slot);
   fprintf(fp, "}\n");

   fclose(fp);
   rename(tmp_path, state_path);
}

VOID
PALPAL_AgentWriteState(
   VOID
)
{
   char state_path[PAL_MAX_PATH];
   char tmp_path[PAL_MAX_PATH];
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

   PALPAL_AgentPath(state_path, sizeof(state_path), "state.json");
   PALPAL_AgentPath(tmp_path, sizeof(tmp_path), "state.json.tmp");
   fp = fopen(tmp_path, "w");
   if (!fp)
   {
      return;
   }

   fprintf(fp, "{");
   fprintf(fp, "\"game_instance_id\":\"%s\",", g_agent_game_instance_id);
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
   rename(tmp_path, state_path);
}
