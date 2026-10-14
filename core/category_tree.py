"""Breadth-first category traversal shared by reading and transfer discovery."""

from collections import deque


def walk_subcategories(*, category, max_depth, fetch_children, stopped,
                       accept_children=None, on_snapshot=None, on_progress=None, on_enter=None):
    if max_depth < 0 or stopped():
        return [], {}
    visited = {category.casefold()}
    queue = deque([(category, 0)])
    found, depths = [], {}
    processed = 0
    while queue and not stopped():
        current, level = queue.popleft()
        if on_enter:
            on_enter(current, level, len(found), processed, len(queue))
        children, counts = fetch_children(current, level, len(found), processed, len(queue))
        unseen = []
        for child in children:
            key = child.casefold()
            if key not in visited:
                visited.add(key)
                unseen.append(child)
        accepted = accept_children(unseen, level + 1) if accept_children else unseen
        for child in accepted:
            key = child.casefold()
            found.append(child)
            depths[key] = level + 1
            if level < max_depth and counts.get(key) != 0:
                queue.append((child, level + 1))
        processed += 1
        if on_snapshot and (processed == 1 or processed % 10 == 0 or not queue):
            on_snapshot(found)
        if on_progress and (processed == 1 or processed % 25 == 0 or not queue):
            on_progress(processed, len(found))
    return found, depths
