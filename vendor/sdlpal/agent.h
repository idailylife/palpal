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

VOID
PALPAL_AgentPollAction(
   VOID
);

VOID
PALPAL_AgentWriteState(
   VOID
);

PAL_C_LINKAGE_END

#endif
