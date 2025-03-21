import logging
import time
import re
import uuid
import datetime
from typing import Tuple, Dict, Any, Optional
from modules.database import get_reminder_collection
from modules.time_extractor import extract_time
from modules.user_interaction import update_conversation_state, get_conversation_state
from modules.meta_context import get_meta_context
from modules.prompts import PromptManager
from modules import ollama_service

reminder_collection = get_reminder_collection()

def process_reminder_intent(chat_id: int, user_name: str, message: str) -> Dict[str, Any]:
    """
    Process a reminder intent, maintaining context across turns.
    Returns a decision dict with collected information and action to take.
    """
    current_state = get_conversation_state(chat_id)
    timestamp = time.time()
    
    # Extract existing reminder details if in a multi-turn conversation
    reminder_details = current_state.get("details", {}) if current_state else {}
    
    # Try to extract time information from the current message
    has_time_info = bool(re.search(r'\bat\b|\bin\b|tomorrow|today|[0-9]+(:[0-9]+)?(\s*[ap]m)?', message.lower()))
    
    # Decision dictionary to return
    decision = {
        "intent": "reminder",
        "original_message": message,
        "user_id": chat_id,
        "user_name": user_name,
        "timestamp": timestamp
    }
    
    # Process first-turn complete reminder
    if has_time_info:
        time_match = re.search(r'\b(at|in|tomorrow|today|[0-9]+(:[0-9]+)?(\s*[ap]m)?)', message.lower())
        if time_match:
            time_part = time_match.group(0)
            due_time, time_str = extract_time(time_part, timestamp)
            
            # Remove time references to get the message content
            content = re.sub(r'\bremind me\b|\breminder\b|\bat\b.*|\bin\b.*|\btomorrow\b.*|\btoday\b.*', '', message, flags=re.IGNORECASE)
            content = content.strip()
            
            if content and len(content) > 2:
                # We have both time and content
                decision.update({
                    "time_info": time_part,
                    "due_time": due_time,
                    "time_str": time_str,
                    "reminder_message": content,
                    "ready_to_create": True
                })
                
                # Update the conversation state
                update_conversation_state(chat_id, "reminder", {
                    "time_info": time_part,
                    "due_time": due_time,
                    "time_str": time_str,
                    "reminder_message": content
                })
                
                return decision
    
    # Handle partial information cases
    if has_time_info:
        due_time, time_str = extract_time(message, timestamp)
        reminder_details["time_info"] = message
        reminder_details["due_time"] = due_time
        reminder_details["time_str"] = time_str
        
        decision["time_info"] = message
        decision["due_time"] = due_time
        decision["time_str"] = time_str
        
        # If we already have content, we're ready
        if "reminder_message" in reminder_details and reminder_details["reminder_message"]:
            decision["reminder_message"] = reminder_details["reminder_message"]
            decision["ready_to_create"] = True
    else:
        # Assume this message is the reminder content
        reminder_details["reminder_message"] = message
        decision["reminder_message"] = message
        
        # If we already have time info, we're ready
        if "time_info" in reminder_details and reminder_details["time_info"]:
            decision["time_info"] = reminder_details["time_info"]
            decision["due_time"] = reminder_details.get("due_time")
            decision["time_str"] = reminder_details.get("time_str")
            decision["ready_to_create"] = True
    
    # Update conversation state with our collected information
    update_conversation_state(chat_id, "reminder", reminder_details, increment_turn=True)
    
    return decision

def create_reminder(decision: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Create a reminder based on the decision dictionary.
    Returns (success, message) tuple.
    """
    if not decision.get("ready_to_create", False):
        # If not ready, try to extract time from the message
        message = decision.get("original_message", "")
        timestamp = decision.get("timestamp", time.time())
        
        # Check for basic time patterns
        if "in" in message.lower() and any(word in message.lower() for word in ["minute", "min", "hour", "day"]):
            # Extract time using time_extractor
            due_time, time_str = extract_time(message, timestamp)
            decision["due_time"] = due_time
            decision["time_str"] = time_str
            
            # Extract reminder text by removing time references
            content = re.sub(r'\bremind me\b|\breminder\b|\bin\b.*|\bminute[s]?\b.*|\bhour[s]?\b.*|\bday[s]?\b.*', '', message, flags=re.IGNORECASE)
            content = content.strip()
            
            if content and len(content) > 2:
                decision["reminder_message"] = content
                decision["ready_to_create"] = True
        
        # If still not ready, report what's missing
        if not decision.get("ready_to_create", False):
            missing = []
            if "time_info" not in decision and "due_time" not in decision:
                missing.append("time")
            if "reminder_message" not in decision:
                missing.append("message")
            
            return False, f"Can't create reminder yet. Missing: {', '.join(missing)}"
    
    chat_id = decision.get("user_id")
    user_name = decision.get("user_name", "User")
    reminder_message = decision.get("reminder_message", "")
    timestamp = decision.get("timestamp", time.time())
    due_time = decision.get("due_time")
    time_str = decision.get("time_str", "")
    
    if not all([chat_id, reminder_message, due_time]):
        return False, "Missing essential reminder information"
    
    # Generate a unique ID
    reminder_id = f"reminder-{chat_id}-{int(timestamp)}"
    
    try:
        # Store reminder with correct chat_id (as string to avoid type issues)
        reminder_collection.add(
            documents=[reminder_message],
            metadatas=[{
                "chat_id": str(chat_id),
                "user_name": user_name,
                "created_at": str(timestamp),
                "message": reminder_message,
                "due_at": str(due_time),
                "time_str": time_str,
                "completed": "false"
            }],
            ids=[reminder_id]
        )
        
        # Format the due time for display
        due_dt = datetime.datetime.fromtimestamp(due_time)
        time_phrase = f"at {due_dt.strftime('%I:%M %p')}" if "at" in time_str else time_str
        
        logging.info(f"Created reminder '{reminder_message}' for chat {chat_id} {time_phrase}")
        
        # Reset the conversation state since we're done with this reminder
        update_conversation_state(chat_id, None)
        
        # Log to meta-context
        meta_context = get_meta_context()
        meta_context.log_event("reminder", "reminder_created", {
            "timestamp": timestamp,
            "chat_id": chat_id,
            "message": reminder_message,
            "due_at": due_time,
            "time_str": time_str,
            "reminder_id": reminder_id
        })
        
        # Only use LLM if we have time for a better user experience
        if due_time - time.time() > 60:  # If reminder is at least 1 minute away
            # Create a more natural confirmation using the LLM
            prompt = PromptManager.format_prompt(
                "reminder_creation_confirmation",
                message=reminder_message,
                time=time_phrase,
                user_name=user_name
            )
            
            # Get a natural confirmation message
            confirmation = ollama_service.process_message(
                message=prompt,
                system_role="reminder"
            )
            
            # Post-process and validate
            confirmation = PromptManager.post_process_response(confirmation)
            
            # Use generated confirmation if it seems valid
            if confirmation and "remind" in confirmation.lower() and len(confirmation) > 20:
                return True, confirmation
    
    except Exception as e:
        logging.error(f"Error creating reminder: {e}")
        return False, "I had trouble setting that reminder. Please try again."
    
    # Fallback to standard message
    return True, f"✅ I'll remind you about '{reminder_message}' {time_phrase}."

def check_due_reminders():
    """Check for reminders that are due and return them."""
    current_time = time.time()
    
    try:
        # Query for due reminders
        results = reminder_collection.get(
            where={"completed": "false"},
            include=["metadatas", "documents"]
        )
        
        due_reminders = []
        
        if results and results.get('ids'):
            for i, reminder_id in enumerate(results['ids']):
                metadata = results['metadatas'][i]
                message = results['documents'][i]
                
                # Check if due (convert string to float)
                due_at = float(metadata.get('due_at', 0))
                if due_at <= current_time:
                    due_reminders.append({
                        'id': reminder_id,
                        'chat_id': metadata.get('chat_id'),
                        'message': message,
                        'user_name': metadata.get('user_name', 'User')
                    })
                    
                    # Mark as completed
                    reminder_collection.update(
                        ids=[reminder_id],
                        metadatas=[{**metadata, "completed": "true"}]
                    )
                    
                    # Log to meta-context
                    meta_context = get_meta_context()
                    meta_context.log_event("reminder", "reminder_triggered", {
                        "timestamp": current_time,
                        "chat_id": metadata.get('chat_id'),
                        "message": message,
                        "reminder_id": reminder_id
                    })
        
        return due_reminders
    
    except Exception as e:
        logging.error(f"Error checking due reminders: {e}")
        return []