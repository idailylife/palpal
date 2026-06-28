/* Agent bridge for PALPAL automation. */

#ifndef PALPAL_AGENT_H
#define PALPAL_AGENT_H

#include "common.h"

PAL_C_LINKAGE_BEGIN

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
PALPAL_AgentWriteState(
   VOID
);

VOID
PALPAL_AgentWriteFrame(
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

PAL_C_LINKAGE_END

#endif
