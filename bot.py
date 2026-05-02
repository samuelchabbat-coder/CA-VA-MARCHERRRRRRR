import discord
from discord.ext import commands
import json, os, asyncio, random

# ══════════════════════════════════════════════════════
#  ⚙️  CONFIG — MODIFIE CES VALEURS
# ══════════════════════════════════════════════════════
GUILD_ID          = 1162542486779072664
CATEGORY_ID       = 1500100508453441736
QUEUE_VC_ID       = 1500100693028110406
LOBBY_VC_ID       = 1500112332163121262
LEADERBOARD_CH_ID = 1500112197827825694
TOKEN             = os.getenv("TOKEN", "")

QUEUE_SIZE    = 6      
LOBBY_WAIT    = 15     
ELO_DEFAULT   = 1000
ELO_K         = 32
VOTE_TIMEOUT  = 600    
CLEANUP_DELAY = 15     
VETO_TIMEOUT  = 60     

DATA_FILE = "data.json"

GRADES = [
    (1500, "⬛ Master",   discord.Color.from_str("#B9F2FF"), "Master"),
    (1400, "💎 Diamond",  discord.Color.from_str("#00BFFF"), "Diamond"),
    (1300, "💚 Emerald",  discord.Color.from_str("#50C878"), "Emerald"),
    (1200, "🩶 Platinum", discord.Color.from_str("#E5E4E2"), "Platinum"),
    (1100, "🥇 Gold",     discord.Color.from_str("#FFD700"), "Gold"),
    (0,    "⬜ Silver",   discord.Color.from_str("#C0C0C0"), "Silver"),
]

# ══════════════════════════════════════════════════════
#  💾  LOGIQUE DONNÉES
# ══════════════════════════════════════════════════════
def load_data():
    if not os.path.exists(DATA_FILE): return {"players": {}, "match_counter": 0}
    with open(DATA_FILE) as f: return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f: json.dump(data, f, indent=2)

def get_player(data, uid):
    k = str(uid)
    if k not in data["players"]:
        data["players"][k] = {"elo": ELO_DEFAULT, "wins": 0, "losses": 0, "pending_vote": None}
    return data["players"][k]

def get_grade(elo):
    for threshold, label, color, role_name in GRADES:
        if elo >= threshold: return label, color, role_name
    return "⬜ Silver", discord.Color.from_str("#C0C0C0"), "Silver"

def elo_calc(winner_elo, loser_elo):
    exp_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    delta = round(ELO_K * (1 - exp_w))
    return delta, -delta

# ══════════════════════════════════════════════════════
#  🤖  BOT INIT & QUEUE
# ══════════════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
queue: list[int] = []
active_matches: dict[int, dict] = {}

async def check_queue_full():
    global queue
    if len(queue) >= QUEUE_SIZE:
        players = queue[:QUEUE_SIZE]
        del queue[:QUEUE_SIZE]
        asyncio.create_task(launch_match(players))

# ══════════════════════════════════════════════════════
#  🎯  INTERFACES (BOUTONS)
# ══════════════════════════════════════════════════════

class PickButton(discord.ui.Button):
    def __init__(self, uid, parent):
        guild = bot.get_guild(GUILD_ID)
        member = guild.get_member(uid)
        label = member.display_name[:20] if member else str(uid)
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.uid = uid
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.cap2:
            return await interaction.response.send_message("Seul le Capitaine 2 peut choisir !", ephemeral=True)
        
        if self.uid in self.parent.picked: return
        
        self.parent.picked.append(self.uid)
        self.disabled = True
        self.style = discord.ButtonStyle.success
        self.label = f"✅ {self.label}"

        if len(self.parent.picked) < 2:
            await interaction.response.edit_message(view=self.parent)
        else:
            self.parent.stop()
            await interaction.response.edit_message(content="✅ **Draft terminée !**", view=self.parent)
            match = active_matches[self.parent.match_id]
            match["team2"] = [self.parent.cap2] + self.parent.picked
            match["team1"] = [u for u in match["players"] if u not in match["team2"]]
            await finalize_draft(interaction.channel, self.parent.match_id)

class DraftView(discord.ui.View):
    def __init__(self, match_id, cap2, pool):
        super().__init__(timeout=120)
        self.match_id = match_id
        self.cap2 = cap2
        self.picked = []
        for uid in pool:
            self.add_item(PickButton(uid, self))

class VetoView(discord.ui.View):
    def __init__(self, match_id, players, cap1, cap2):
        super().__init__(timeout=VETO_TIMEOUT)
        self.match_id, self.players, self.cap1, self.cap2 = match_id, players, cap1, cap2
        self.vetos, self.accepts, self.resolved = set(), set(), False

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, btn):
        if interaction.user.id not in self.players: return
        self.accepts.add(interaction.user.id)
        await interaction.response.defer()
        await self._check(interaction.channel)

    @discord.ui.button(label="❌ Veto", style=discord.ButtonStyle.red)
    async def veto(self, interaction: discord.Interaction, btn):
        if interaction.user.id not in self.players: return
        self.vetos.add(interaction.user.id)
        await interaction.response.send_message(f"🚫 {interaction.user.mention} vote Veto ({len(self.vetos)}/4)", delete_after=5)
        await self._check(interaction.channel)

    async self._check(self, channel):
        if self.resolved: return
        if len(self.vetos) >= 4:
            self.resolved = True; self.stop()
            await channel.send("⚡ **Veto majoritaire !** Nouveau tirage...")
            await launch_veto(channel, self.match_id, self.players)
        elif len(self.accepts) >= 4:
            self.resolved = True; self.stop()
            await start_draft(channel, self.match_id, self.cap1, self.cap2)

class ResultView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=VOTE_TIMEOUT)
        self.match_id, self.votes, self.done = match_id, {}, False

    @discord.ui.button(label="🔴 Team 1 gagne", style=discord.ButtonStyle.danger)
    async def t1(self, it, btn): await self._vote(it, "team1")
    @discord.ui.button(label="🔵 Team 2 gagne", style=discord.ButtonStyle.primary)
    async def t2(self, it, btn): await self._vote(it, "team2")

    async def _vote(self, it, team):
        match = active_matches.get(self.match_id)
        if not match or it.user.id not in (match["team1"] + match["team2"]): return
        self.votes[it.user.id] = team
        v1 = list(self.votes.values()).count("team1")
        v2 = list(self.votes.values()).count("team2")
        await it.response.send_message(f"Vote enregistré : {v1 if team=='team1' else v2}/4", ephemeral=True)
        if (v1 >= 4 or v2 >= 4) and not self.done:
            self.done = True; self.stop()
            await process_result(it.channel, self.match_id, "team1" if v1 >= 4 else "team2")

# ══════════════════════════════════════════════════════
#  ⚔️  LOGIQUE MATCH
# ══════════════════════════════════════════════════════

async def launch_match(players):
    guild = bot.get_guild(GUILD_ID)
    lobby = guild.get_channel(LOBBY_VC_ID)
    for uid in players:
        m = guild.get_member(uid)
        if m and m.voice: await m.move_to(lobby)
    
    await asyncio.sleep(LOBBY_WAIT)
    in_lobby = [m.id for m in lobby.members]
    leaved = [u for u in players if u not in in_lobby]
    
    if leaved:
        for u in players:
            m = guild.get_member(u)
            if m and m.voice: await m.move_to(guild.get_channel(QUEUE_VC_ID))
        return

    data = load_data(); data["match_counter"] += 1
    mid = data["match_counter"]
    for u in players: get_player(data, u)["pending_vote"] = mid
    save_data(data)

    cat = guild.get_channel(CATEGORY_ID)
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    for u in players: overwrites[guild.get_member(u)] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    
    ch = await guild.create_text_channel(f"match-{mid}-draft", category=cat, overwrites=overwrites)
    active_matches[mid] = {"players": players, "draft_channel": ch.id}
    await launch_veto(ch, mid, players)

async def launch_veto(ch, mid, players):
    c1, c2 = random.sample(players, 2)
    view = VetoView(mid, players, c1, c2)
    await ch.send(f"⚔️ **Match #{mid}**\n🔴 Cap 1 : <@{c1}>\n🔵 Cap 2 : <@{c2}>\n\nValidez ou Veto (4 votes requis).", view=view)

async def start_draft(ch, mid, c1, c2):
    match = active_matches[mid]
    match.update({"cap1": c1, "cap2": c2})
    pool = [u for u in match["players"] if u not in (c1, c2)]
    view = DraftView(mid, c2, pool)
    await ch.send(f"🔵 <@{c2}>, clique sur les **2 joueurs** pour ton équipe :", view=view)

async def finalize_draft(ch, mid):
    match = active_matches[mid]
    guild = bot.get_guild(GUILD_ID)
    cat = guild.get_channel(CATEGORY_ID)
    
    for t in ["team1", "team2"]:
        over = {guild.default_role: discord.PermissionOverwrite(connect=False)}
        for u in match[t]: over[guild.get_member(u)] = discord.PermissionOverwrite(connect=True)
        vc = await guild.create_voice_channel(f"{'🔴' if t=='team1' else '🔵'} Match {mid}", category=cat, overwrites=over)
        match[f"{t}_vc"] = vc.id
        for u in match[t]:
            m = guild.get_member(u)
            if m and m.voice: await m.move_to(vc)

    await ch.send("⚔️ **Match lancé !** Votez le résultat quand vous avez fini.", view=ResultView(mid))

async def process_result(ch, mid, win_team):
    match = active_matches[mid]; data = load_data()
    winners, losers = match[win_team], match["team2" if win_team=="team1" else "team1"]
    
    avg_w = sum(get_player(data, u)["elo"] for u in winners)/3
    avg_l = sum(get_player(data, u)["elo"] for u in losers)/3
    dw, dl = elo_calc(avg_w, avg_l)

    res = [f"🏆 **Victoire : {'🔴 Team 1' if win_team=='team1' else '🔵 Team 2'}**\n"]
    for u in winners + losers:
        p = get_player(data, u); old = p["elo"]
        is_w = u in winners
        p["elo"] = max(0, p["elo"] + (dw if is_w else dl))
        if is_w: p["wins"] += 1 
        else: p["losses"] += 1
        p["pending_vote"] = None
        grade, _, _ = get_grade(p["elo"])
        res.append(f"{'✅' if is_w else '❌'} <@{u}> : {old} → **{p['elo']}** ({dw if is_w else dl}) | {grade}")

    save_data(data); await ch.send("\n".join(res))
    await update_leaderboard(bot.get_guild(GUILD_ID), data)
    await asyncio.sleep(CLEANUP_DELAY); await cleanup_match(mid)

async def cleanup_match(mid):
    m = active_matches.pop(mid, None)
    if not m: return
    guild = bot.get_guild(GUILD_ID)
    for cid in [m.get("draft_channel"), m.get("team1_vc"), m.get("team2_vc")]:
        if cid: 
            c = guild.get_channel(cid)
            if c: await c.delete()

# ══════════════════════════════════════════════════════
#  🏆  LEADERBOARD & EVENTS
# ══════════════════════════════════════════════════════

async def update_leaderboard(guild, data):
    ch = guild.get_channel(LEADERBOARD_CH_ID)
    if not ch: return
    ranked = sorted(data["players"].items(), key=lambda x: x[1]["elo"], reverse=True)
    txt = "```🏆 TOP 20 ELO\n" + "-"*40 + "\n"
    for i, (uid, p) in enumerate(ranked[:20]):
        m = guild.get_member(int(uid))
        name = (m.display_name if m else uid)[:15]
        g, _, _ = get_grade(p["elo"])
        txt += f"#{i+1:<2} {name:<15} {p['elo']:>4} {g}\n"
    txt += "```"
    async for msg in ch.history(limit=5):
        if msg.author == bot.user: return await msg.edit(content=txt)
    await ch.send(txt)

@bot.event
async def on_ready():
    print(f"✅ {bot.user} en ligne")
    guild = bot.get_guild(GUILD_ID)
    q_vc = guild.get_channel(QUEUE_VC_ID)
    if q_vc:
        for m in q_vc.members:
            if not m.bot and m.id not in queue: queue.append(m.id)
        await check_queue_full()

@bot.event
async def on_voice_state_update(m, b, a):
    if m.bot: return
    if a.channel and a.channel.id == QUEUE_VC_ID:
        if m.id not in queue: 
            queue.append(m.id); await check_queue_full()
    elif b.channel and b.channel.id == QUEUE_VC_ID:
        if m.id in queue: queue.remove(m.id)

bot.run(TOKEN)
