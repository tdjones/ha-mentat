"""The AI Task entity: ai_task.generate_data answered by a decision model, field by field.

A question sensor answers on its schedule, so a script that starts on a change
may still read the answer from before it. This entity asks when it is called.
The task's instructions are the state, and each field of its structure is one
question about it, all sent in one request through the same budget check as
the actions.

Home Assistant lets a task leave out `structure` and get free text back. A
decision model writes no prose, so such a task is refused before anything is
sent. Image attachments, camera snapshots among them, go into the request as
pictures beside the instructions. Any other attachment is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from homeassistant.components import ai_task, conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from jevclient import Answer, ChoiceAnswer, JevResponse, NoulAnswer, ScoreAnswer

from .const import DOMAIN, UID_AI_TASK
from .coordinator import unique_id
from .entity import build_device_info
from .fields import RESERVED_KEY, questions_from_structure, values_from_answers
from .pictures import (
    Pictures,
    async_gather_pictures,
    async_read_file,
    attach,
    check_image_count,
)
from .services import async_ask, check_room, max_images

if TYPE_CHECKING:
    from . import MentatConfigEntry

# Each task is one request, and the entity holds nothing between them.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MentatConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Give the entry its AI Task entity.

    A task is answered when it is called, so there is no coordinator to wait
    for and nothing to fetch before the entity is added.
    """
    add_entities([MentatAITaskEntity(entry)], update_before_add=False)


class MentatAITaskEntity(ai_task.AITaskEntity):
    """Answers a structured task about whatever the caller writes. No watch takes part."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "decision"
    _attr_supported_features = (
        ai_task.AITaskEntityFeature.GENERATE_DATA | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
    )

    def __init__(self, entry: MentatConfigEntry) -> None:
        self._attr_device_info = build_device_info(entry)
        self._attr_unique_id = unique_id(entry.entry_id, UID_AI_TASK)
        self._entry = entry

    @override
    async def _async_generate_data(
        self, task: ai_task.GenDataTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenDataTaskResult:
        structure = task.structure
        if structure is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="task_needs_structure",
                translation_placeholders={"task": task.name},
            )
        # Refused fields cost nothing: this runs before any attachment is read.
        questions = questions_from_structure(structure)
        pictures = await self._async_attachments(task)
        response = await async_ask(
            self.hass,
            self._entry,
            attach(task.instructions, pictures),
            questions,
            pictures=pictures or None,
            task=task.name,
        )
        values = values_from_answers(structure, response.answers)
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=values | {RESERVED_KEY: _receipt(response)},
        )

    async def _async_attachments(self, task: ai_task.GenDataTask) -> Pictures:
        """The task's attachments as pictures. Home Assistant wrote each to disk first.

        A spent budget, too many attachments or one that is no image is refused
        before a single file is read.
        """
        pictures = Pictures.empty(self._entry.runtime_data.client.dialect)
        attachments = task.attachments or []
        if not attachments:
            return pictures
        check_room(self._entry, task.name)
        check_image_count(len(attachments), max_images(self._entry))
        strangers = [item for item in attachments if not item.mime_type.startswith("image/")]
        if strangers:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="attachment_not_image",
                translation_placeholders={
                    "attachment": strangers[0].media_content_id,
                    "mime_type": strangers[0].mime_type,
                },
            )
        fetches = [(item.media_content_id, self._async_read(item)) for item in attachments]
        return await async_gather_pictures(self.hass, fetches, pictures.dialect)

    async def _async_read(self, item: conversation.Attachment) -> tuple[bytes, dict[str, Any]]:
        """One attachment's bytes, and the label the state gives them."""
        media_id = item.media_content_id
        # Capped like a file source: a media source can resolve to any file the
        # user may browse, a video among them.
        data = await async_read_file(self.hass, item.path, media_id)
        # media-source://camera/camera.front_door ends in the camera's id, which
        # is how an instruction would name it.
        return data, {"name": media_id.rpartition("/")[2]}


def _receipt(response: JevResponse) -> dict[str, Any]:
    """What the values leave out: who answered, at what cost, and how sure.

    A boolean field comes back as a bare true, which hides whether the model was
    at 0.51 or 0.99. That number is what tells an answer worth acting on from a
    guess, so it travels beside the values.
    """
    certainty = {key: _certainty_of(answer) for key, answer in response.answers.items()}
    billed = response.usage.input_tokens
    return {
        "model": response.model,
        "input_tokens": billed,
        "latency_ms": round(response.latency_ms),
        "answers": certainty,
    }


def _rounded(probabilities: dict[str, float]) -> dict[str, float]:
    return {option: round(share, 4) for option, share in probabilities.items()}


def _certainty_of(answer: Answer) -> dict[str, Any]:
    """How sure one answer is.

    A noul is itself a probability and carries no confidence. How sure it is
    shows in how far it sits from a half.
    """
    match answer:
        case NoulAnswer():
            return {"probability": round(answer.noul, 4)}
        case ScoreAnswer():
            spread = {"confidence": round(answer.confidence, 4)}
            return spread | {
                "probabilities": _rounded(answer.probabilities),
                "nearest_level": answer.nearest_level,
            }
        case ChoiceAnswer():
            return {
                "confidence": round(answer.confidence, 4),
                "probabilities": _rounded(answer.probabilities),
            }
    # A box: no field asks for one.
    return {}
