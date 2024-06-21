import pyautogui
import time
import keyboard

# Define the keybinds for each loadout
keybinds = {
    'loadout1': '1',
    'loadout2': '2',
    'loadout3': '3',
    'loadout4': '4',
    'loadout5': '5',
    'loadout6': '6',
    'loadout7': '7',
    'loadout8': '8',
    'loadout9': '9',
    'loadout11': '-',
    'loadout12': '+'
}

# Define the starting position for loadout 1
start_x = 144
start_y = 338

# Define the offset between loadouts
offset = 97

# Function to perform the macro actions
def perform_macro(loadout):
    # Press F1
    pyautogui.press('f1')
    time.sleep(0.45) # Wait for the action to complete

    # Press left arrow key
    pyautogui.press('left')
    time.sleep(0.45) # Wait for the action to complete

    # Calculate the x and y coordinates
    x_offset = (loadout - 1) % 2
    y_offset = (loadout - 1) // 2
    x = start_x + x_offset * offset
    y = start_y + y_offset * offset

    # Move the cursor to the desired position
    pyautogui.moveTo(x, y)

    # Left click
    pyautogui.click()
    time.sleep(0.45) # Wait for the action to complete

    # Press Esc
    pyautogui.press('f1')

# Print the keybinds
print("Loadout Keybinds:")
for loadout, key in keybinds.items():
    print(f"{loadout}: {key}")

def on_key_press(event):
    # Comment next 2 lines if not keypad keys
    if not event.is_keypad:
        return
    for loadout, key in keybinds.items():
        if str(event.name) == key:
            perform_macro(int(loadout[7:]))  # Extract the loadout number from the keybinds
            
keyboard.on_press(on_key_press)

keyboard.wait("pause")
