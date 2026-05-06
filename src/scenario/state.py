"""State-machine definition for the resignation scenario."""

from statemachine import State, StateMachine

from scenario.model import CasePhase


class ResignationCaseMachine(StateMachine):
    """Main workflow phase machine.

    Guards and field mutations live outside this class. The machine only
    encodes legal phase transitions.
    """

    awaiting_hr_confirm = State(
        "Awaiting HR confirm",
        value=CasePhase.AWAITING_HR_CONFIRM,
        initial=True,
    )
    handover_and_recovery = State(
        "Handover and recovery",
        value=CasePhase.HANDOVER_AND_RECOVERY,
    )
    awaiting_hr_signoff = State(
        "Awaiting HR signoff",
        value=CasePhase.AWAITING_HR_SIGNOFF,
    )
    closed = State("Closed", value=CasePhase.CLOSED, final=True)

    hr_confirm = awaiting_hr_confirm.to(handover_and_recovery)
    tl_done_wait = handover_and_recovery.to.itself()
    tl_done_ready = handover_and_recovery.to(awaiting_hr_signoff)
    ops_done_wait = handover_and_recovery.to.itself()
    ops_done_ready = handover_and_recovery.to(awaiting_hr_signoff)
    hr_sign = awaiting_hr_signoff.to(closed)
    mark_tl_timeout = handover_and_recovery.to.itself()
    mark_ops_timeout = handover_and_recovery.to.itself()
