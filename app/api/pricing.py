from fastapi import APIRouter, HTTPException, status

from app.schemas import BracketRead, PricingScheduleRead
from app.services import pricing

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/schedule", response_model=PricingScheduleRead)
def read_schedule(key: str = pricing.CURRENT_SCHEDULE) -> PricingScheduleRead:
    """The rate tables, for the "how pricing works" copy. Defaults to the
    schedule new trips get; pass an older key to see what a trip created
    under it is paying. No sign-in needed."""
    try:
        rates = pricing.schedule(key)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such price schedule") from None
    return PricingScheduleRead(
        key=rates.key,
        included_members=rates.included_members,
        trip_brackets=[BracketRead(**vars(b)) for b in rates.trip_brackets],
        member_brackets=[BracketRead(**vars(b)) for b in rates.member_brackets],
    )
