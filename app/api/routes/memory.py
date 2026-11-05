"""Direct access to SirisAI's memory store.

The primary way memory gets written and read is the LLM calling the
`remember`/`recall` tools mid-conversation (see
app/plugins/builtin/memory.py). These endpoints exist alongside that for
inspecting and seeding memory directly -- without going through a chat turn.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_api_key
from app.core.siris import SirisAI, get_siris
from app.core.user_context import resolve_user_id

router = APIRouter(
    prefix="/siris/memory", tags=["memory"], dependencies=[Depends(require_api_key), Depends(resolve_user_id)]
)


class MemoryCreateRequest(BaseModel):
    content: str


class MemoryEntryResponse(BaseModel):
    id: int
    position: int
    content: str
    created_at: str
    recall_count: int
    last_recalled_at: str | None


@router.post("", response_model=MemoryEntryResponse)
def create_memory(request: MemoryCreateRequest, siris: SirisAI = Depends(get_siris)) -> MemoryEntryResponse:
    return MemoryEntryResponse(**siris.memory.remember(request.content))


@router.get("", response_model=list[MemoryEntryResponse])
def list_memory(
    query: str | None = None, limit: int = 20, siris: SirisAI = Depends(get_siris)
) -> list[MemoryEntryResponse]:
    return [MemoryEntryResponse(**entry) for entry in siris.memory.recall(query=query, limit=limit)]


@router.put("/{memory_id}", response_model=MemoryEntryResponse)
def update_memory(memory_id: int, request: MemoryCreateRequest, siris: SirisAI = Depends(get_siris)) -> MemoryEntryResponse:
    """Correct a memory's words (4.4), from SirisOS's Manage page. Keeps its id, date and recall count."""
    try:
        updated = siris.memory.update(memory_id, request.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return MemoryEntryResponse(**updated)


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: int, siris: SirisAI = Depends(get_siris)) -> None:
    """Direct REST delete, for the Memory tab's own delete button and
    manual cleanup -- the model has its own equivalent via the
    delete_memory tool (see MemoryStore.delete())."""
    if not siris.memory.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
