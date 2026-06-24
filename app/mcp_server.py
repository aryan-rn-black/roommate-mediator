import json
import os
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("RoommateMediatorMCPServer")

DATA_FILE = os.path.join(os.path.dirname(__file__), "roommate_data.json")

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    
    # Default data
    default_data = {
        "balances": {
            "Alice": 0.0,
            "Bob": 0.0,
            "Charlie": 0.0
        },
        "chores": [
            {"chore": "Empty kitchen trash", "assigned_to": "Alice", "completed": False},
            {"chore": "Clean the bathroom", "assigned_to": "Bob", "completed": False},
            {"chore": "Sweep the living room", "assigned_to": "Charlie", "completed": False},
            {"chore": "Wash common dishes", "assigned_to": "Alice", "completed": True}
        ]
    }
    save_data(default_data)
    return default_data

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

@mcp.tool()
def get_balances() -> str:
    """Get the current net balances of all roommates (positive means owed money, negative means they owe)."""
    data = load_data()
    balances = data["balances"]
    lines = [f"{roommate}: ${balance:+.2f}" for roommate, balance in balances.items()]
    return "\n".join(lines)

@mcp.tool()
def log_expense(payer: str, amount: float, description: str) -> str:
    """Log a shared household expense. Splits the cost equally among roommates and updates balances.
    
    Args:
        payer: The name of the roommate who paid (e.g. Alice, Bob, Charlie).
        amount: The total amount paid.
        description: What the expense was for.
    """
    data = load_data()
    balances = data["balances"]
    
    # Normalize payer capitalization
    payer_norm = payer.strip().capitalize()
    if payer_norm not in balances:
        return f"Error: Roommate '{payer}' is not registered."
        
    num_roommates = len(balances)
    split_share = amount / num_roommates
    
    for roommate in balances:
        if roommate == payer_norm:
            balances[roommate] += amount - split_share
        else:
            balances[roommate] -= split_share
            
    save_data(data)
    return f"Successfully logged expense: '{description}' of ${amount:.2f} paid by {payer_norm}.\nEach roommate's share: ${split_share:.2f}.\nUpdated Balances:\n" + get_balances()

@mcp.tool()
def get_chore_schedule() -> str:
    """Retrieve the current schedule and status of household chores."""
    data = load_data()
    chores = data["chores"]
    lines = []
    for c in chores:
        status = "✅ Completed" if c["completed"] else "❌ Pending"
        lines.append(f"- {c['chore']} (Assigned: {c['assigned_to']}) - {status}")
    return "\n".join(lines)

@mcp.tool()
def mark_chore_done(chore_name: str, roommate: str) -> str:
    """Mark a specific chore as completed by a roommate.
    
    Args:
        chore_name: The name of the chore (e.g., 'Clean the bathroom').
        roommate: The roommate completing the chore.
    """
    data = load_data()
    chores = data["chores"]
    
    found = False
    roommate_norm = roommate.strip().capitalize()
    for c in chores:
        if chore_name.lower() in c["chore"].lower():
            c["completed"] = True
            c["assigned_to"] = roommate_norm  # Ensure it is credited to them
            found = True
            break
            
    if not found:
        return f"Chore '{chore_name}' not found. Current chores:\n" + get_chore_schedule()
        
    save_data(data)
    return f"Chore '{chore_name}' marked as completed by {roommate_norm}.\nUpdated Chore Schedule:\n" + get_chore_schedule()

@mcp.tool()
def calculate_utility_split(total_bill: float, num_roommates: int, spike_reason: str = None) -> str:
    """Calculate the split for a utility bill and provide mediating explanations if there's a spike.
    
    Args:
        total_bill: The total cost of the utility bill.
        num_roommates: The number of roommates splitting the bill.
        spike_reason: Optional reason for a bill spike (e.g. 'heavy AC use in summer' or 'running space heater').
    """
    split = total_bill / num_roommates
    explanation = f"Utility Bill Split:\n- Total: ${total_bill:.2f}\n- Split per person ({num_roommates} roommates): ${split:.2f}\n"
    if spike_reason:
        explanation += f"\nNote on Bill Spike:\nThe bill is higher due to: {spike_reason}.\nProposal: We recommend paying equal shares this time, but establishing guidelines (like smart thermostat limits or individual space heater allowances) to keep future costs low."
    return explanation

@mcp.tool()
def generate_payment_link(recipient: str, amount: float, note: str) -> str:
    """Generate a Venmo request/payment deep link.
    
    Args:
        recipient: The roommate name or Venmo username (e.g. Alice).
        amount: The request amount in dollars (e.g. 30.00).
        note: A description note for the charge (e.g. 'Dinner split').
    """
    import urllib.parse
    recipient_escaped = urllib.parse.quote(recipient)
    note_escaped = urllib.parse.quote(note)
    link = f"https://venmo.com/?txn=charge&recipients={recipient_escaped}&amount={amount:.2f}&note={note_escaped}"
    return f"Venmo Payment Link Generated:\n{link}"

@mcp.tool()
def rotate_chores() -> str:
    """Rotate the assigned roommates for all chores weekly to ensure fairness."""
    data = load_data()
    chores = data["chores"]
    roommates = list(data["balances"].keys())
    
    for c in chores:
        current_idx = roommates.index(c["assigned_to"]) if c["assigned_to"] in roommates else 0
        next_idx = (current_idx + 1) % len(roommates)
        c["assigned_to"] = roommates[next_idx]
        c["completed"] = False
        
    save_data(data)
    return "Chores rotated successfully for the new week.\nNew Chore Schedule:\n" + get_chore_schedule()

@mcp.tool()
def check_house_rules(complaint: str) -> str:
    """Check a roommate complaint or house policy query against the official house rules agreement.
    
    Args:
        complaint: The roommate issue or question (e.g., 'Charlie had a guest stay for 5 days' or 'Bob was loud at midnight').
    """
    rules_path = os.path.join(os.path.dirname(__file__), "house_rules.txt")
    if os.path.exists(rules_path):
        with open(rules_path, "r") as f:
            rules = f.read()
    else:
        rules = "No house rules established yet."
        
    return f"Official House Rules Agreement:\n\n{rules}\n\n[End of Agreement]\nPlease analyze the complaint against these rules and determine if a violation occurred, then suggest a compromise."

if __name__ == "__main__":
    mcp.run(transport="stdio")
