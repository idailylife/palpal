"""Observed movement and player-space planning; no future script interpretation."""
from __future__ import annotations

import copy
from collections import OrderedDict, deque
import threading
import time


def room_key(state):
    return (state.get('game_instance_id'), state.get('scene_id'), state.get('map_id'),
            (state.get('room') or {}).get('key') or state.get('room_key'),
            state.get('logical_room_id') if not (state.get('room') or {}).get('key') else None)


class MotionHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = OrderedDict()
        self.instance = None
        self.frame = None
        self.serial = 0
        self.last_room = None

    def clear(self):
        with self.lock:
            self.rooms.clear()
            self.frame = None

    def observe(self, state):
        with self.lock:
            now = time.monotonic()
            frame = state.get('raw_frame') if state.get('raw_frame') is not None else state.get('frame')
            instance = state.get('game_instance_id')
            if instance != self.instance or (frame is not None and self.frame is not None and frame < self.frame):
                self.clear()
            self.instance, self.frame = instance, frame
            key = room_key(state)
            if key != self.last_room and self.last_room in self.rooms:
                for track in self.rooms[self.last_room].values():
                    track['present'] = False
            self.last_room = key
            room = self.rooms.setdefault(key, {})
            self.rooms.move_to_end(key)
            while len(self.rooms) > 8:
                self.rooms.popitem(last=False)
            trigger = state.get('trigger_observation') or {}
            present = set()
            for obj in state.get('objects') or []:
                ident = obj.get('id')
                present.add(ident)
                track = room.setdefault(ident, {'points': deque(maxlen=512), 'truncated': False, 'present': False})
                point = {k: obj.get(k) for k in ('x', 'y', 'state', 'sprite_id', 'trigger_mode', 'trigger_script')}
                point['present'] = int(obj.get('state') or 0) > 0
                old = track['points'][-1] if track['points'] else None
                continuous_samples = track['present'] and now - track.get('last_seen', now) <= 1.0
                continuous = continuous_samples and bool(old and old['present']) and point['present']
                changed = bool(old and (old['x'], old['y']) != (point['x'], point['y']))
                if not continuous or changed:
                    track['quiet_since'] = now
                if changed and continuous:
                    track['last_moved_frame'] = frame
                    track['recent_motion'] = True
                elif not continuous:
                    track['recent_motion'] = False
                track['last_seen'] = now
                track['sample_frame'] = frame
                if not continuous_samples or (old and old['present'] != point['present']):
                    track['continuity_token'] = self.serial + 1
                    track['present'] = False
                if old and all(old.get(k) == v for k, v in point.items()) and track['present']:
                    continue
                if old and (old['x'], old['y']) != (point['x'], point['y']) and track['present']:
                    track['moved_at'] = time.time()
                self.serial += 1
                point.update(frame=frame, sequence=self.serial,
                             trigger_generation=trigger.get('generation'),
                             trigger_object_id=trigger.get('event_object_id'),
                             gap=bool(old and not track['present']))
                track['truncated'] |= len(track['points']) == 512
                track['points'].append(point)
                track['present'] = True
            for ident in room.keys() - present:
                room[ident]['present'] = False

    def view(self, state, ident, offset=None, limit=32, since=None):
        with self.lock:
            track = self.rooms.get(room_key(state), {}).get(ident)
            if not track:
                return {'available': False, 'current_status': 'unknown', 'history_missing': True, 'points': [], 'complete': True}
            fresh = track['present'] and time.monotonic() - track['last_seen'] <= 1.0
            visible = bool(track['points'] and track['points'][-1]['present'])
            quiet = max(0.0, track['last_seen'] - track['quiet_since']) if fresh and visible else 0.0
            status = ('stationary' if quiet >= .4 else 'moving' if track.get('recent_motion') else 'unknown') if fresh and visible else 'unknown'
            points = list(track['points'])
            if since is not None:
                # Include the previous point as the starting position of this action.
                index = next((i for i, p in enumerate(points) if p['sequence'] > since), len(points))
                points = points[max(0, index - 1):]
            # Keep every turn, discontinuity and state/trigger change. Collinear
            # intermediate samples can be omitted without inventing a path.
            compressed = []
            for p in points:
                if len(compressed) >= 2:
                    a, b = compressed[-2:]
                    same = all(a.get(k) == b.get(k) == p.get(k) for k in
                               ('state', 'sprite_id', 'trigger_mode', 'trigger_script', 'trigger_generation', 'present'))
                    dx, dy = b['x'] - a['x'], b['y'] - a['y']
                    ex, ey = p['x'] - b['x'], p['y'] - b['y']
                    if same and not b['gap'] and not p['gap'] and dx * ey == dy * ex and dx * ex + dy * ey > 0:
                        compressed.pop()
                compressed.append(p)
            limit = max(1, min(int(limit), 128))
            offset = max(0, len(compressed) - limit) if offset is None else max(0, int(offset))
            page = compressed[offset:offset + limit]
            moved = any((a['x'], a['y']) != (b['x'], b['y']) and not b['gap']
                        for a, b in zip(points, points[1:]))
            return copy.deepcopy({'available': True, 'source': 'observed_state_stream',
                'history_scope': 'since_sampling_started', 'truncated': track['truncated'], 'moved': moved,
                'moved_at': track.get('moved_at'), 'moved_scope': 'historical',
                'current_status': status, 'sample_frame': track.get('sample_frame'),
                'last_moved_frame': track.get('last_moved_frame'),
                'position_is_current': fresh and visible,
                'continuity_token': track.get('continuity_token'),
                'position_is_stationary': status == 'stationary',
                'observation_windows': list(track.get('windows', [])),
                'movement_rule': 'not_inferred',
                'observed_stationary_seconds': round(quiet, 3),
                'latest_action_association': track.get('association'),
                'start': points[0] if points else None, 'end': points[-1] if points else None,
                'points': page, 'offset': offset, 'total': len(compressed),
                'next_offset': offset + len(page), 'complete': offset + len(page) >= len(compressed)})

    def associate(self, state, ident, action, evidence, since, before=None):
        """Retain observed action/trigger association, without predicting motion."""
        with self.lock:
            track = self.rooms.get(room_key(state), {}).get(ident)
            if track is None:
                return None
            motion = self.view(state, ident, since=(evidence or {}).get('motion_sequence', since), limit=16)
            summary = {'action': action, 'trigger': copy.deepcopy(evidence),
                       'player_start': copy.deepcopy((before or {}).get('party_position')),
                       'player_end': copy.deepcopy(state.get('party_position')),
                       'association': 'native_trigger_confirmed' if evidence else 'observed_during_action',
                       **{k: motion[k] for k in ('moved', 'start', 'end', 'current_status',
                                                'observed_stationary_seconds', 'sample_frame')}}
            track['association'] = summary
            track.setdefault('windows', deque(maxlen=16)).append(copy.deepcopy(summary))
            return copy.deepcopy(summary)

    def record_window(self, state, ident, summary):
        with self.lock:
            track = self.rooms.get(room_key(state), {}).get(ident)
            if track is not None:
                track.setdefault('windows', deque(maxlen=16)).append(copy.deepcopy(summary))

    def changes(self, state, since, object_id=None):
        with self.lock:
            ids = [object_id] if object_id is not None else list(self.rooms.get(room_key(state), {}))
        result = []
        for ident in ids:
            view = self.view(state, ident, since=since, limit=16)
            if view.get('moved') and (view.get('end') or {}).get('present'):
                result.append({'object_id': ident, **view})
        return result[:8]


def recovery_options(debug, state):
    """Safe single steps, ranked by the area reachable without another trigger.

    A blocked landing can separate a small pocket from the main room. Merely
    stepping onto a walkable cell does not distinguish those recovery outcomes.
    Keep every legal candidate; the ranking is geometry, not task progress.
    """
    from palpal.debug_server import decode_nav_grid, grid_node, nav_bit, MOVE_DELTAS, trigger_kind
    if not debug.control_state(state)['navigation_ready'] or not debug.room_state_stable(state):
        return []
    grid = decode_nav_grid(state)
    if grid is None or grid['step'] != (16, 8):
        return []
    pos = state.get('party_position') or {}
    x, y = pos.get('x'), pos.get('y')
    if type(x) is not int or type(y) is not int:
        return []
    start = grid_node(grid, x, y)
    if start is None or nav_bit(grid, start):
        return []
    dynamic_blocked = set(debug.blocking_nodes_by_object(state, grid))
    interactables = [obj for obj in state.get('objects') or []
                     if obj.get('interactable') and int(obj.get('state') or 0) > 0]
    touch_owners = {}
    for obj in interactables:
        for node in debug.touch_boundary_nodes_for_object(obj, grid):
            touch_owners.setdefault(node, set()).add(int(obj['id']))
    blocked = dynamic_blocked | set(touch_owners)
    options = []
    for move, (dx, dy) in MOVE_DELTAS.items():
        nx, ny = x + dx * 16, y + dy * 8
        node = grid_node(grid, nx, ny)
        if node is not None and nav_bit(grid, node) and node not in blocked:
            options.append({'x': nx, 'y': ny, 'move': move, 'steps': 1,
                            'purpose': 'recover_start'})
    if not options:
        return []

    graph = debug.navigation_graph(grid)
    search_goals = {int(obj['id']): debug.goal_nodes(state, grid, obj)[0]
                    for obj in interactables if trigger_kind(obj.get('trigger_mode')) == 'search'}
    components = {}
    for option in options:
        node = grid_node(grid, option['x'], option['y'])
        if node not in components:
            safe, queue, targets = {node}, deque([node]), set()
            while queue:
                for _, neighbor in graph.get(queue.popleft(), ()):
                    if neighbor in blocked:
                        # A trigger is a terminal target, never a corridor.
                        owners = touch_owners.get(neighbor, set())
                        if neighbor not in dynamic_blocked and len(owners) == 1:
                            targets.update(owners)
                    elif neighbor not in safe:
                        safe.add(neighbor)
                        queue.append(neighbor)
            targets.update(ident for ident, goals in search_goals.items() if goals & safe)
            summary = {'safe_reachable_cells': len(safe),
                       'reachable_target_count': len(targets),
                       'reachable_target_ids': sorted(targets)[:10]}
            for member in safe:
                components[member] = summary
        option.update(components[node])
    options.sort(key=lambda option: (-option['safe_reachable_cells'], -option['reachable_target_count']))
    for index, option in enumerate(options):
        option['recommended'] = index == 0
    return options


def route_to_position(debug, state, x, y, face=None):
    from palpal.debug_server import decode_nav_grid, grid_node, nav_bit, world_pos, DIR_TO_BUTTON, MOVE_DELTAS
    grid = decode_nav_grid(state)
    if grid is None:
        return {'ok': False, 'status': 'no_nav_grid'}
    if type(x) is not int or type(y) is not int or (face is not None and face not in MOVE_DELTAS):
        return {'ok': False, 'status': 'invalid_position'}
    pos = state.get('party_position') or {}
    start = grid_node(grid, int(pos.get('x') or 0), int(pos.get('y') or 0))
    goal = grid_node(grid, x, y)
    if goal is None or not nav_bit(grid, goal):
        return {'ok': False, 'status': 'invalid_position'}
    if start is None or not nav_bit(grid, start):
        option = next((o for o in recovery_options(debug, state)
                       if (o['x'], o['y']) == (x, y) and face in (None, o['move'])), None)
        if option is None:
            return {'ok': False, 'status': 'bad_start'}
        return {'ok': True, 'status': 'reachable', 'recovery': True,
                'cell': {'x': x, 'y': y}, 'face': face, 'steps': 1,
                'path': [{'move': option['move'], 'x': x, 'y': y, 'trigger_ids': []}]}

    blocked = set(debug.blocking_nodes_by_object(state, grid))
    if goal in blocked:
        return {'ok': False, 'status': 'position_blocked'}
    boundaries = {}
    for obj in state.get('objects') or []:
        if obj.get('interactable') and int(obj.get('state') or 0) > 0:
            for node in debug.touch_boundary_nodes_for_object(obj, grid):
                boundaries.setdefault(node, []).append(int(obj['id']))
    graph = debug.navigation_graph(grid)
    initial = (start, DIR_TO_BUTTON.get(int(state.get('party_direction') or 0), 'down'))
    queue, parent = deque([initial]), {initial: None}
    end = None
    while queue:
        current = queue.popleft()
        node, direction = current
        if node == goal and (face is None or direction == face):
            end = current
            break
        # A touch boundary is an endpoint, never a corridor through a trigger.
        if node in boundaries and node != start:
            continue
        neighbors = dict(graph.get(node, ()))
        for move, delta in MOVE_DELTAS.items():
            dest = neighbors.get(move)
            if dest is None or dest in blocked:
                # Native arrow keys turn against a physical obstacle without moving.
                dest = node
            nxt = (dest, move)
            if nxt in parent:
                continue
            parent[nxt] = (current, move)
            queue.append(nxt)
    if end is None:
        return {'ok': False, 'status': 'unreachable_position_or_facing'}
    steps = []
    while parent[end] is not None:
        previous, move = parent[end]
        px, py = world_pos(grid, end[0])
        steps.append({'move': move, 'x': px, 'y': py, 'trigger_ids': boundaries.get(end[0], [])})
        end = previous
    steps.reverse()
    return {'ok': True, 'status': 'reachable' if steps else 'arrived',
            'cell': {'x': x, 'y': y}, 'face': face, 'steps': len(steps), 'path': steps}


def navigation_observation(debug, state):
    identity = debug.room_identity(state)
    return {'game_instance_id': state.get('game_instance_id'),
            'room_id': identity.get('id') if identity else None,
            'frame': state.get('frame'), 'raw_frame': state.get('raw_frame'),
            'position': copy.deepcopy(state.get('party_position'))}


def route_diagnostic(debug, state, target_id, route):
    """Explain one hypothetical path, never authorize or execute it."""
    from palpal.debug_server import decode_nav_grid, grid_node, nav_bit, trigger_kind, transition_candidate
    result = {'observation': navigation_observation(debug, state),
              'status': route.get('status'), 'candidate_only': True,
              'guarantees_passage': False}
    if route.get('status') not in {'blocked_by_transition', 'blocked_by_interactable', 'unreachable_static'}:
        return result
    grid = decode_nav_grid(state)
    target = debug.target_object(state, target_id)
    if grid is None or target is None:
        return result
    pos = state.get('party_position') or {}
    start = grid_node(grid, int(pos.get('x') or 0), int(pos.get('y') or 0))
    if start is None or not nav_bit(grid, start):
        return result
    goals, _ = debug.goal_nodes(state, grid, target)
    touches = {int(obj['id']): obj for obj in state.get('objects') or []
               if obj.get('interactable') and int(obj.get('state') or 0) > 0
               and trigger_kind(int(obj.get('trigger_mode') or 0)) == 'touch'}
    boundaries = {}
    for ident, obj in touches.items():
        for node in debug.touch_boundary_nodes_for_object(obj, grid):
            boundaries.setdefault(node, []).append(ident)
    # Relax only touch objects. Other physical occupants remain obstacles.
    blockers = debug.blocking_nodes_by_object(state, grid, target_id)
    blocked = {node for node, obj in blockers.items() if int(obj['id']) not in touches}
    candidate = debug.route_between_nodes(grid, start, goals, blocked)
    if candidate is None:
        result['reason'] = 'no_candidate_with_touch_boundaries_relaxed'
        if route.get('blocker'):
            result['blocker'] = route['blocker']
        return result
    navigation = {t['id']: t for t in debug.navigation(state).get('targets') or []}
    prefix = []
    for point in candidate.get('nodes') or []:
        prefix.append({'x': point['x'], 'y': point['y']})
        node = grid_node(grid, point['x'], point['y'])
        if node == start:
            continue
        ids = sorted(ident for ident in boundaries.get(node, []) if ident != target_id)
        if not ids:
            continue
        triggers = []
        for ident in ids:
            info = navigation.get(ident, {})
            destination = debug.transition_destination_for_state(state, ident)
            triggers.append({'id': ident, 'status': info.get('status', 'unknown'),
                             'executable': info.get('status') in {'reachable', 'transition', 'blocked_by_interactable'},
                             'transition_candidate': transition_candidate(touches[ident]) or bool(destination.get('known_destination')),
                             'known_destination': bool(destination.get('known_destination')),
                             'interaction_state': info.get('interaction_state'),
                             'agent_priority': info.get('agent_priority')})
        result.update(reason='intervening_touch_boundary', first_triggers=triggers,
                      path_to_first_trigger=prefix, stops_at_first_trigger=True)
        return result
    result['reason'] = 'no_intervening_touch_on_candidate'
    return result


def exploration_options(debug, state, limit=5):
    """Offer safe unvisited frontier cells using current geometry, without writes."""
    from palpal.debug_server import decode_nav_grid, grid_node, nav_bit, parse_node_key, world_pos
    result = {'ok': True, 'observation': navigation_observation(debug, state), 'options': []}
    if not debug.control_state(state)['navigation_ready'] or not debug.room_state_stable(state):
        return {**result, 'status': 'waiting_for_control'}
    grid = decode_nav_grid(state)
    if grid is None:
        return {**result, 'status': 'no_nav_grid'}
    pos = state.get('party_position') or {}
    start = grid_node(grid, int(pos.get('x') or 0), int(pos.get('y') or 0))
    if start is None or not nav_bit(grid, start):
        return {**result, 'status': 'bad_start',
                'options': recovery_options(debug, state)[:limit],
                'meaning': 'Prefer the recommended recovery unless a current target justifies another component; counts describe current safe geometry, not task progress. Use move_to once, then refresh navigation.'}
    with debug.exploration_lock:
        data = debug.read_exploration()
        identity = debug.room_identity(state)
        room = (data.get('rooms') or {}).get(identity['id'], {}) if identity else {}
        visited = {parse_node_key(key) for key in room.get('visited') or []}
    visited.add(start)
    blocked = set(debug.blocking_nodes_by_object(state, grid))
    for obj in state.get('objects') or []:
        if obj.get('interactable') and int(obj.get('state') or 0) > 0:
            blocked.update(debug.touch_boundary_nodes_for_object(obj, grid))
    order, distance, _ = debug.navigation_distance_field(grid, start, blocked)
    safe = set(order) - blocked
    unvisited = safe - visited
    graph = debug.navigation_graph(grid)
    frontier = {node for node in unvisited
                if any(neighbor in visited for _, neighbor in graph.get(node, ()))}
    candidates = []
    while frontier:
        seed = min(frontier)
        frontier.remove(seed)
        group, queue = {seed}, deque([seed])
        while queue:
            for _, neighbor in graph.get(queue.popleft(), ()):
                if neighbor in frontier:
                    frontier.remove(neighbor)
                    group.add(neighbor)
                    queue.append(neighbor)
        node = min(group, key=lambda n: (distance[n], world_pos(grid, n)))
        x, y = world_pos(grid, node)
        candidates.append((distance[node], x, y, len(group), node))
    for _, x, y, size, node in sorted(candidates):
        route = debug.route_to_position(state, x, y)
        if not route.get('ok') or any(step['trigger_ids'] for step in route['path']):
            continue
        result['options'].append({'x': x, 'y': y, 'steps': route['steps'],
                                  'frontier_group_size': size,
                                  'nearby_unexplored': sum(n in unvisited for _, n in graph.get(node, ()))})
        if len(result['options']) >= limit:
            break
    result['status'] = 'ok' if result['options'] else 'no_safe_frontier'
    result['meaning'] = 'Current safe candidates only; an empty list does not establish room completion.'
    return result


def preview_placement(debug, state, item_id, x=None, y=None, face=None):
    from palpal.debug_server import decode_nav_grid, grid_node, nav_bit, MOVE_DELTAS
    item = next((i for i in state.get('inventory') or [] if i.get('item_id') == item_id), None)
    if not item or not item.get('usable') or int(item.get('amount') or 0) - int(item.get('amount_in_use') or 0) <= 0:
        return {'ok': False, 'error': 'item_not_available'}
    reqs = [r for r in (item.get('use_effect') or {}).get('requirements', []) if r.get('kind') == 'place_event_object']
    if not reqs:
        return {'ok': False, 'error': 'placement_requirement_missing'}
    pos = state.get('party_position') or {}
    x, y = (pos.get('x'), pos.get('y')) if x is None and y is None else (x, y)
    if type(x) is not int or type(y) is not int or (face is not None and face not in MOVE_DELTAS):
        return {'ok': False, 'error': 'invalid_position'}
    grid = decode_nav_grid(state)
    if grid is None:
        return {'ok': False, 'error': 'no_nav_grid'}
    objects = {o.get('id'): o for o in state.get('objects') or []}
    options = []
    for req in reqs:
        for direction in ([face] if face else MOVE_DELTAS):
            dx, dy = MOVE_DELTAS[direction]
            landing = {'x': x + dx * 16, 'y': y + dy * 8}
            node = grid_node(grid, landing['x'], landing['y'])
            terrain_valid = node is not None and nav_bit(grid, node)
            route = route_to_position(debug, state, x, y, direction)
            in_scene = req.get('event_object_id') in objects
            options.append({'event_object_id': req.get('event_object_id'), 'standing_position': {'x': x, 'y': y},
                            'face': direction, 'landing_position': landing, 'terrain_valid': terrain_valid,
                            'object_in_scene': in_scene, 'placement_valid': in_scene and terrain_valid,
                            'standing_reachable': route['ok'], 'standing_status': route['status'],
                            'conditional': req.get('conditional', False)})
    return {'ok': True, 'item_id': item_id, 'room': list(room_key(state)), 'options': options,
            'task_effectiveness': 'unknown', 'basis': 'current_geometry_not_future_script',
            'requirements_complete': (item.get('use_effect') or {}).get('requirements_complete', False)}
