"""Skeleton test for the task_store fixture (runs before session 1 work)."""

from app import TaskStore


def test_store_starts_empty():
    store = TaskStore()
    assert store.list_tasks() == []
