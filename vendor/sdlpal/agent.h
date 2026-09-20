/* Agent bridge for PALPAL automation. */

#ifndef PALPAL_AGENT_H
#define PALPAL_AGENT_H

#include "common.h"

PAL_C_LINKAGE_BEGIN

/* Pixel provenance for visible scene objects; never exposes hidden sprites. */
VOID PALPAL_AgentSceneBegin(VOID);
VOID PALPAL_AgentSceneObject(WORD object_id, BOOL clipped);
VOID PALPAL_AgentSceneEnd(VOID);
VOID PALPAL_AgentDrawSpan(SDL_Surface *surface, int x, int y, int count);

BOOL
PALPAL_AgentEnabled(
   VOID
);

INT
PALPAL_AgentSaveSlot(
   VOID
);

BOOL
PALPAL_AgentStartupWaitMode(
   VOID
);

VOID
PALPAL_AgentBeginStartupWait(
   VOID
);

BOOL
PALPAL_AgentStartupReady(
   VOID
);

INT
PALPAL_AgentStartupSlot(
   VOID
);

BOOL
PALPAL_AgentHeadless(
   VOID
);

VOID
PALPAL_AgentPollAction(
   VOID
);

VOID
PALPAL_AgentAfterFrame(
   VOID
);

VOID
PALPAL_AgentBattleHPRedistributed(VOID);

VOID
PALPAL_AgentBattleBegin(
   VOID
);

VOID
PALPAL_AgentBattleCommandReady(
   WORD       wPlayerIndex
);

VOID
PALPAL_AgentBattleRewardBegin(
   VOID
);

VOID
PALPAL_AgentBattleEnding(
   VOID
);

VOID
PALPAL_AgentBattleEnd(
   INT        iResult
);

VOID
PALPAL_AgentVisibleActionBegin(
   BOOL       fPlayer,
   WORD       wActorIndex,
   INT        iActionType,
   WORD       wObjectID,
   SHORT      sTarget
);

VOID
PALPAL_AgentVisibleRoundEndBegin(
   VOID
);

VOID
PALPAL_AgentVisibleActionTarget(SHORT sTarget);

VOID
PALPAL_AgentVisibleActionEnd(
   VOID
);

VOID
PALPAL_AgentVisibleNumber(
   BOOL       fEnemy,
   WORD       wTargetIndex,
   BOOL       fMP,
   SHORT      sChange
);

DWORD
PALPAL_AgentBattleGeneration(
   VOID
);

DWORD
PALPAL_AgentBattleCommandGeneration(
   VOID
);

BOOL
PALPAL_AgentBattleAwaitingAck(
   VOID
);

VOID
PALPAL_AgentWriteState(
   VOID
);

VOID
PALPAL_AgentWriteFrame(
   VOID
);

VOID
PALPAL_AgentPollFrame(
   VOID
);

VOID
PALPAL_AgentObserveDialogStart(
   WORD       wEventObjectID
);

VOID
PALPAL_AgentObserveDialogText(
   INT        iMessageID,
   WORD       wEventObjectID,
   LPCWSTR    lpszText
);

VOID PALPAL_AgentPrepareChoice(WORD wEventObjectID, WORD wScriptEntry);
VOID PALPAL_AgentBeginChoice(WORD wEventObjectID, WORD wScriptEntry);
VOID PALPAL_AgentObserveChoiceSelection(WORD wSelectedIndex);
VOID PALPAL_AgentEndChoice(WORD wSelectedIndex);

VOID
PALPAL_AgentBeginBuyMenu(
   WORD       wStoreNum,
   WORD       wSelectedItem
);

VOID
PALPAL_AgentObserveBuyMenuSelection(
   WORD       wSelectedItem
);

VOID
PALPAL_AgentObserveBuyMenuPhase(
   BOOL       fConfirming
);

VOID
PALPAL_AgentObserveBuyMenuResult(
   WORD       wItem,
   BOOL       fPurchased,
   DWORD      dwCashBefore,
   DWORD      dwCashAfter,
   INT        iOwnedBefore,
   INT        iOwnedAfter
);

VOID
PALPAL_AgentEndBuyMenu(
   VOID
);

VOID PALPAL_AgentBeginSellMenu(VOID);
VOID PALPAL_AgentObserveSellMenuSelection(WORD wSelectedItem);
VOID PALPAL_AgentObserveSellMenuPhase(BOOL fConfirming);
VOID PALPAL_AgentObserveSellMenuResult(
   WORD       wItem,
   BOOL       fSold,
   DWORD      dwCashBefore,
   DWORD      dwCashAfter,
   INT        iOwnedBefore,
   INT        iOwnedAfter
);
VOID PALPAL_AgentEndSellMenu(VOID);

/* Record that a concrete event object's trigger script ran.  This is used by
 * the companion to attribute a later room change without guessing from map
 * geometry alone. */
VOID
PALPAL_AgentBeginTriggerScript(
   WORD       wEventObjectID,
   WORD       wTriggerScript
);

VOID
PALPAL_AgentEndTriggerScript(
   WORD       wEventObjectID
);

PAL_C_LINKAGE_END

#endif
