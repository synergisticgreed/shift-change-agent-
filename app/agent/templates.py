"""Plain replies for every outcome. Used when the LLM is down/over budget (FR-11).
Data Flow: state["result"] {outcome, ...} -> render() -> draft text -> reply node (-> LLM polish)."""

ASK = {
    "trip_type": "Is this for your pickup or your drop?",
    "date": "Which day is this for (today or tomorrow)?",
    "new_time": "What time would you like instead (for example 20:00)?",
}

TEMPLATES = {
    "applied": "Done! Your {trip} on {date} is now at {time} (cab {cab}).",
    "cancelled": "Done. Your {trip} on {date} is cancelled.",
    "no_booking": "I can't find a {trip} booked for you on {date}.",
    "no_cab": "There's no {trip} cab at {time}. Available times: {options}. Which one works?",
    "cab_full": "The {time} cab is full. The nearest free slot is {alt_time}. Want it?",
    "seat_taken": "That seat was just taken. The next free slot is {alt_time}. Want it?",
    "no_seats": "Sorry, every {trip} cab on {date} is full.",
    "cutoff": ("Changes must be made {cutoff_h}h before the trip, and your {trip} is at {trip_time}. "
               "Want me to send it to your transport manager for approval?"),
    "pending": "Your request #{request_id} is with your transport manager. I'll update you here.",
    "approved": "Good news: your manager approved request #{request_id}. Your {trip} is now at {time}.",
    "approved_cancel": "Good news: your manager approved request #{request_id}. Your {trip} on {date} is cancelled.",
    "same_time": "You're already on the {time} {trip} on {date}, so nothing needed changing.",
    "declined":"Your manager declined request #{request_id}. Your trip stays as it was.",
    "status": "Your latest request #{request_id} is {status}.",
    "no_requests": "You have no shift-change requests yet.",
    "duplicate": "You already sent that exact request, so I didn't create another one.",
    "tool_error": "I couldn't reach the roster system. Nothing was changed. Please try again in a minute.",
    "nothing": "Okay, nothing was changed.",
    "unclear": "Sorry, I didn't get that. You can change, cancel or check a pickup/drop.",
}


def render(result: dict) -> str:
    kind = result["outcome"]
    if kind == "clarify":
        return ASK.get(result["missing"][0], TEMPLATES["unclear"])
    return TEMPLATES[kind].format(**result)
