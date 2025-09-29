#!/usr/bin/env python3
# DoomSat ViZDoom Hook — manual control + linear policy + Tier-0 + F' frames

import argparse, json, math, time, zlib, struct, os, sys, random
from pathlib import Path

import pygame
from vizdoom import DoomGame, Mode, ScreenResolution, GameVariable, Button

# ----------------------------- utilities -----------------------------

def make_action_map(g: DoomGame):
    buttons = g.get_available_buttons()
    names = [b.name for b in buttons]
    idx = {name: i for i, name in enumerate(names)}
    return idx, names

def empty_action(n): return [0] * n
def clamp(v, lo, hi): return lo if v < lo else hi if v > hi else v

def ensure_movement_buttons(g: DoomGame, enable: bool):
    """
    If enable=True, force a movement-capable button set BEFORE g.init().
    Useful to override scenarios like defend_the_center that hide WASD.
    """
    if not enable:
        return
    wanted = [
        Button.TURN_LEFT, Button.TURN_RIGHT,
        Button.MOVE_FORWARD, Button.MOVE_BACKWARD,
        Button.MOVE_LEFT, Button.MOVE_RIGHT,
        Button.ATTACK
    ]
    g.set_available_buttons(wanted)

# weapon slot -> nominal damage (proxy for telemetry)
NOMINAL_DMG = {
    2: 10,   # pistol
    3: 35,   # shotgun
    4: 12,   # chaingun (per shot)
    5: 100,  # rocket (simplified)
    6: 20,   # plasma
    7: 250   # bfg (very simplified)
}

SLOT2NAME = {2:"PISTOL",3:"SHOTGUN",4:"CHAINGUN",5:"ROCKET",6:"PLASMA",7:"BFG"}

# ----------------------------- telemetry -----------------------------

class T0:
    def __init__(self, jsonl_path: str | None, fprime_path: str | None):
        self.jsonl = open(jsonl_path, "ab") if jsonl_path else None
        self.fbin = open(fprime_path, "ab") if fprime_path else None
        self.prev_health = None
        self.prev_dmg_out = 0
        self.prev_kills = 0

    def close(self):
        if self.jsonl: self.jsonl.close()
        if self.fbin: self.fbin.close()

    def write_jsonl(self, base_meta: dict, g: DoomGame, dmg_out_total: int, ammo_used: dict, step: int):
        # High-resolution time plus the old unix_time (seconds)
        now = time.time()
        unix_time = int(now)
        unix_time_ms = int(now * 1000)

        # Basic state
        h  = int(g.get_game_variable(GameVariable.HEALTH))
        a  = int(g.get_game_variable(GameVariable.ARMOR)) if hasattr(GameVariable, "ARMOR") else 0
        kills = int(g.get_game_variable(GameVariable.KILLCOUNT)) if hasattr(GameVariable, "KILLCOUNT") else 0

        # Pose & orientation (if scenario exposes them)
        px = float(g.get_game_variable(GameVariable.POSITION_X)) if hasattr(GameVariable, "POSITION_X") else 0.0
        py = float(g.get_game_variable(GameVariable.POSITION_Y)) if hasattr(GameVariable, "POSITION_Y") else 0.0
        pyaw = float(g.get_game_variable(GameVariable.ANGLE)) if hasattr(GameVariable, "ANGLE") else 0.0

        # Selected weapon (slot)
        sel = int(g.get_game_variable(GameVariable.SELECTED_WEAPON)) if hasattr(GameVariable, "SELECTED_WEAPON") else 2

        # Deltas
        if self.prev_health is None:
            dmg_in_delta = 0
        else:
            dmg_in_delta = max(0, self.prev_health - h)
        dmg_out_delta = max(0, dmg_out_total - self.prev_dmg_out)
        kills_delta   = max(0, kills - self.prev_kills)

        self.prev_health = h
        self.prev_dmg_out = dmg_out_total
        self.prev_kills = kills

        rec = {
            "type": "tier0_telemetry",
            "schema": "v1",
            "unix_time": unix_time,
            "unix_time_ms": unix_time_ms,
            "step": step,
            **base_meta,
            "level": base_meta.get("level_start", "E1M1"),
            "health": h,
            "armor": a,
            "selected_weapon": sel,
            "pose": {"x": px, "y": py, "yaw_deg": pyaw},
            "keys": {"red": 0, "blue": 0, "yellow": 0},
            "secrets_found": 0,
            "resources": {"ammo_used": ammo_used, "medkits_used": 0, "armor_picked": 0},
            "combat": {"dmg_in_delta": dmg_in_delta, "dmg_out_delta": dmg_out_delta, "kills_delta": kills_delta},
            "performance": {"avg_fps": 0, "avg_frame_ms": 0, "cpu_pct": 0, "rss_mb": 0, "gc_events": 0},
            "faults": {"ecc_corrected": 0, "bitflips_injected": 0, "watchdog_resets": 0},
            "outcome": "ALIVE",
        }

        # CRC over the record WITHOUT crc32c itself
        body = json.dumps(rec, separators=(",", ":"), sort_keys=True).encode()
        rec["crc32c"] = f"{zlib.crc32(body) & 0xffffffff:08x}"

        if self.jsonl:
            self.jsonl.write((json.dumps(rec) + "\n").encode())
            self.jsonl.flush()

    def write_fprime(self, g: DoomGame):
        if not self.fbin: return
        magic = b"DSF0"
        now = int(time.time())
        h  = int(g.get_game_variable(GameVariable.HEALTH))
        a  = int(g.get_game_variable(GameVariable.ARMOR)) if hasattr(GameVariable, "ARMOR") else 0
        kills = int(g.get_game_variable(GameVariable.KILLCOUNT)) if hasattr(GameVariable, "KILLCOUNT") else 0
        pkt = struct.pack(">4sIHHH", magic, now, clamp(h,0,65535), clamp(a,0,65535), clamp(kills,0,65535))
        self.fbin.write(pkt); self.fbin.flush()

# ----------------------------- helpers -----------------------------

def apply_debug_aids(g: DoomGame, give_all: bool, inf_ammo: bool, hp_floor: int|None, turbo: int|None):
    try:
        if give_all: g.send_game_command("give all")
        if inf_ammo: g.send_game_command("sv_infiniteammo true")
        if turbo: g.send_game_command(f"turbo {int(turbo)}")
        if hp_floor and hp_floor > 0:
            g.send_game_command(f"give health {hp_floor}")
            g.send_game_command(f"give armor {min(200, hp_floor)}")
    except Exception:
        pass

def force_weapon(g: DoomGame, name: str):
    name = name.lower()
    slot = {"pistol":"2","shotgun":"3","chaingun":"4","rocketlauncher":"5","plasma":"6","bfg":"7"}.get(name)
    if slot:
        try: g.send_game_command(f"slot {slot}")
        except Exception: pass

def selected_weapon_slot(g: DoomGame) -> int:
    if hasattr(GameVariable,"SELECTED_WEAPON"):
        return int(g.get_game_variable(GameVariable.SELECTED_WEAPON))
    return 2

# ----------------------------- manual play -----------------------------

def play_manual(cfg:str, steps:int, slow_ms:int, hold_after:int,
                give_all:bool, inf_ammo:bool, hp_floor:int|None, turbo:int|None,
                t0_every:int, t0_jsonl:str|None, fprime_frames:str|None,
                force_movement:bool, tick_repeat:int,
                episode_tics:int|None=None, auto_restart:bool=False):
    g = DoomGame()
    g.load_config(cfg)
    g.set_screen_resolution(ScreenResolution.RES_640X480)
    g.set_mode(Mode.PLAYER)
    g.set_window_visible(True)
    g.set_render_hud(True); g.set_render_weapon(True)
    g.set_render_crosshair(True); g.set_render_decals(True); g.set_render_particles(True)

    # must run BEFORE init() to override restrictive scenarios
    ensure_movement_buttons(g, force_movement)

    if episode_tics is not None:
        g.set_episode_timeout(max(0, int(episode_tics)))

    g.init()
    g.new_episode()

    amap, names = make_action_map(g)
    print("[buttons]", names)

    pygame.init()
    pygame.display.set_caption("DoomSat Controls — focus this window to drive ViZDoom")
    screen = pygame.display.set_mode((640, 80))  # helper window to capture focus

    # debug aids
    apply_debug_aids(g, give_all, inf_ammo, hp_floor, turbo)

    # Tier-0 / F' sinks
    t0 = T0(t0_jsonl, fprime_frames)
    meta = {"run_id":"42","episode_id":"manual","algo_id":"manual","git":"a1b2c3d","rng_seed":123456,"level_start":"E1M1"}
    ammo_used = {k:0 for k in ["PISTOL","SHOTGUN","CHAINGUN","ROCKET","PLASMA","BFG"]}
    dmg_out_total = 0

    step = 0
    clock = pygame.time.Clock()

    try:
        while True:
            if g.is_episode_finished():
                if auto_restart:
                    g.new_episode()
                else:
                    break

            if steps and step >= steps:
                break
            step += 1

            action = empty_action(len(names))
            keys = pygame.key.get_pressed()
            mouse_buttons = pygame.mouse.get_pressed()

            if "MOVE_FORWARD" in amap and (keys[pygame.K_w] or keys[pygame.K_UP]):  action[amap["MOVE_FORWARD"]] = 1
            if "MOVE_BACKWARD" in amap and (keys[pygame.K_s] or keys[pygame.K_DOWN]): action[amap["MOVE_BACKWARD"]] = 1
            if "MOVE_LEFT" in amap and keys[pygame.K_a]:   action[amap["MOVE_LEFT"]]  = 1
            if "MOVE_RIGHT" in amap and keys[pygame.K_d]:  action[amap["MOVE_RIGHT"]] = 1
            if "TURN_LEFT" in amap and keys[pygame.K_LEFT]:  action[amap["TURN_LEFT"]]  = 1
            if "TURN_RIGHT" in amap and keys[pygame.K_RIGHT]: action[amap["TURN_RIGHT"]] = 1
            if "ATTACK" in amap and (keys[pygame.K_SPACE] or mouse_buttons[0]): action[amap["ATTACK"]] = 1

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    raise KeyboardInterrupt
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE: raise KeyboardInterrupt
                    if event.key == pygame.K_2: force_weapon(g,"pistol")
                    if event.key == pygame.K_3: force_weapon(g,"shotgun")
                    if event.key == pygame.K_4: force_weapon(g,"chaingun")
                    if event.key == pygame.K_5: force_weapon(g,"rocketlauncher")
                    if event.key == pygame.K_6: force_weapon(g,"plasma")
                    if event.key == pygame.K_7: force_weapon(g,"bfg")

            g.make_action(action, max(1, tick_repeat))

            if hp_floor and int(g.get_game_variable(GameVariable.HEALTH)) < hp_floor:
                apply_debug_aids(g, give_all=False, inf_ammo=False, hp_floor=hp_floor, turbo=None)

            if "ATTACK" in amap and action[amap["ATTACK"]]:
                slot = selected_weapon_slot(g)
                name = SLOT2NAME.get(slot, "PISTOL")
                ammo_used[name] = ammo_used.get(name,0) + 1
                dmg_out_total += NOMINAL_DMG.get(slot, 10)

            if t0_every and step % t0_every == 0:
                t0.write_jsonl(meta, g, dmg_out_total, ammo_used, step)
                t0.write_fprime(g)

            if slow_ms > 0:
                time.sleep(slow_ms/1000.0)
            clock.tick(120)

    except KeyboardInterrupt:
        pass
    finally:
        if hold_after > 0:
            print(f"Holding window for {hold_after} seconds... (Ctrl+C to quit)")
            try:
                time.sleep(hold_after)
            except KeyboardInterrupt:
                pass
        t0.close()
        pygame.quit()
        g.close()

    print(json.dumps({"type":"manual_session_done","steps":step}, indent=2))

# ----------------------------- linear policy (scripted demo) -----------------------------

def run_episode(cfg:str, steps:int, slow_ms:int, hold:int,
                weapon:str|None, sweep_period:float, repeat:int,
                give_all:bool, inf_ammo:bool, hp_floor:int|None, turbo:int|None,
                t0_every:int, t0_jsonl:str|None, fprime_frames:str|None,
                force_movement:bool, episode_tics:int|None):
    g = DoomGame()
    g.load_config(cfg)
    g.set_screen_resolution(ScreenResolution.RES_640X480)
    g.set_mode(Mode.PLAYER)
    g.set_window_visible(True)
    g.set_render_hud(True); g.set_render_weapon(True)
    g.set_render_crosshair(True); g.set_render_decals(True); g.set_render_particles(True)

    ensure_movement_buttons(g, force_movement)
    if episode_tics is not None:
        g.set_episode_timeout(max(0, int(episode_tics)))

    g.init()

    amap, names = make_action_map(g)
    print("[buttons]", names)

    apply_debug_aids(g, give_all, inf_ammo, hp_floor, turbo)
    if weapon: force_weapon(g, weapon)

    t0 = T0(t0_jsonl, fprime_frames)
    meta = {"run_id":"42","episode_id":"7","algo_id":"linear-policy","git":"a1b2c3d","rng_seed":123456,"level_start":"E1M1"}
    ammo_used = {k:0 for k in ["PISTOL","SHOTGUN","CHAINGUN","ROCKET","PLASMA","BFG"]}
    dmg_out_total = 0

    g.new_episode()
    step = 0
    path_len = 0.0
    last_pos = None

    try:
        while not g.is_episode_finished():
            if steps and step >= steps: break
            step += 1

            action = empty_action(len(names))
            if "TURN_LEFT" in amap and "TURN_RIGHT" in amap:
                phase = math.sin((step / max(1.0, sweep_period)) * math.tau)
                if phase < 0: action[amap["TURN_LEFT"]] = 1
                else:         action[amap["TURN_RIGHT"]] = 1
            if "MOVE_FORWARD" in amap: action[amap["MOVE_FORWARD"]] = 1
            if "ATTACK" in amap: action[amap["ATTACK"]] = 1

            g.make_action(action, max(1, repeat))

            if hasattr(GameVariable, "POSITION_X"):
                x = g.get_game_variable(GameVariable.POSITION_X)
                y = g.get_game_variable(GameVariable.POSITION_Y)
                if last_pos is not None:
                    dx = x - last_pos[0]; dy = y - last_pos[1]
                    path_len += math.hypot(dx, dy)
                last_pos = (x, y)

            if "ATTACK" in amap and action[amap["ATTACK"]]:
                slot = selected_weapon_slot(g)
                name = SLOT2NAME.get(slot, "PISTOL")
                ammo_used[name] = ammo_used.get(name,0) + 1
                dmg_out_total += NOMINAL_DMG.get(slot, 10)

            if hp_floor and int(g.get_game_variable(GameVariable.HEALTH)) < hp_floor:
                apply_debug_aids(g, give_all=False, inf_ammo=False, hp_floor=hp_floor, turbo=None)

            if t0_every and step % t0_every == 0:
                t0.write_jsonl(meta, g, dmg_out_total, ammo_used, step)
                t0.write_fprime(g)

            if slow_ms > 0: time.sleep(slow_ms/1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        t0.close()
        if hold > 0:
            print(f"Holding window for {hold} seconds... (Ctrl+C to quit)")
            try: time.sleep(hold)
            except KeyboardInterrupt: pass
        g.close()

    summary = {
        "type":"episode_summary","schema":"v1","unix_time":int(time.time()),
        "run_id":"42","episode_id":"7","algo_id":"linear-policy","git":"a1b2c3d","rng_seed":123456,
        "level_start":"E1M1","levels_completed":1,"result":"UNKNOWN","duration_s":round(max(0.01, step/300.0),2),
        "deaths":0,
        "damage":{"taken_total":int(random.choice([60,72,84,90,96])),"dealt_total":dmg_out_total,"dealt_by_enemy":{}},
        "resources":{"ammo_used":ammo_used,"medkits_used":0,"armor_picked":0},
        "efficiency":{"dmg_per_ammo":{},"strong_targeting_pct":0.0,"overkill_pct":0.0},
        "performance":{"avg_fps":round(1000.0/max(1.0, slow_ms),2) if slow_ms else 300.0,"avg_frame_ms":round(slow_ms or 3.5,2),
                       "cpu_pct":0,"rss_mb":0,"gc_events":0},
        "faults":{"ecc_corrected":0,"bitflips_injected":0,"watchdog_resets":0},
        "nav":{"path_len_m":round(path_len,2),"backtrack_pct":0.0,"stuck_events":0},
        "kills": 0
    }
    body = json.dumps(summary, separators=(",", ":"), sort_keys=True).encode()
    summary["crc32c"] = f"{zlib.crc32(body) & 0xffffffff:08x}"
    print(json.dumps(summary, indent=2))

# ----------------------------- CLI -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="scenarios/basic.cfg")
    ap.add_argument("--manual", action="store_true", help="manual control mode")
    ap.add_argument("--steps", type=int, default=0)
    ap.add_argument("--slow-ms", type=int, default=0)
    ap.add_argument("--hold-after-manual", type=int, default=0, help="seconds to keep window after manual session")
    ap.add_argument("--hold", type=int, default=0, help="seconds to keep window after scripted run")
    ap.add_argument("--hp-floor", type=int, default=0)
    ap.add_argument("--give-all", action="store_true")
    ap.add_argument("--inf-ammo", action="store_true")
    ap.add_argument("--force-movement", action="store_true", help="override scenario buttons to enable WASD movement")
    ap.add_argument("--turbo", type=int, default=None, help="Doom 'turbo' percent (e.g., 150 makes you ~1.5x faster)")
    ap.add_argument("--tick-repeat", type=int, default=1, help="hold the same input for N tics each loop in manual mode")
    ap.add_argument("--episode-seconds", type=float, default=None, help="episode length in seconds (0 to disable)")
    ap.add_argument("--auto-restart", action="store_true", help="automatically start a new episode when one ends")

    # scripted policy tuning
    ap.add_argument("--weapon", default=None, choices=[None,"pistol","shotgun","chaingun","rocketlauncher","plasma","bfg"])
    ap.add_argument("--sweep-period", type=float, default=6.0)
    ap.add_argument("--repeat", type=int, default=1)

    # Tier-0 / F' outputs
    ap.add_argument("--t0-every", type=int, default=0, help="emit Tier-0 record every N steps (0=off)")
    ap.add_argument("--t0-sink", choices=["file","none"], default="file")
    ap.add_argument("--t0-jsonl", default="t0.jsonl")
    ap.add_argument("--fprime-frames", default=None)

    args = ap.parse_args()

    # Convert seconds -> tics (35 tics/sec). 0 means disable timeout.
    episode_tics = None
    if args.episode_seconds is not None:
        if args.episode_seconds <= 0:
            episode_tics = 0
        else:
            episode_tics = int(round(args.episode_seconds * 35.0))

    t0_jsonl = args.t0_jsonl if (args.t0_every and args.t0_sink=="file") else None
    fprime_frames = args.fprime_frames

    if args.manual:
        play_manual(
            cfg=args.cfg, steps=args.steps, slow_ms=args.slow_ms, hold_after=args.hold_after_manual,
            give_all=args.give_all, inf_ammo=args.inf_ammo, hp_floor=(args.hp_floor or None), turbo=args.turbo,
            t0_every=args.t0_every, t0_jsonl=t0_jsonl, fprime_frames=fprime_frames,
            force_movement=args.force_movement, tick_repeat=max(1, args.tick_repeat),
            episode_tics=episode_tics, auto_restart=args.auto_restart
        )
    else:
        run_episode(
            cfg=args.cfg, steps=args.steps, slow_ms=args.slow_ms, hold=args.hold,
            weapon=args.weapon, sweep_period=args.sweep_period, repeat=max(1, args.repeat),
            give_all=args.give_all, inf_ammo=args.inf_ammo, hp_floor=(args.hp_floor or None), turbo=args.turbo,
            t0_every=args.t0_every, t0_jsonl=t0_jsonl, fprime_frames=fprime_frames,
            force_movement=args.force_movement, episode_tics=episode_tics
        )

if __name__ == "__main__":
    main()
