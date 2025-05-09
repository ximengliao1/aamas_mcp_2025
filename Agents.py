# -*- coding: utf-8 -*-
# @Time    : 15/04/2025 15:02
# @Author  : mmai
# @FileName: Solver
# @Software: PyCharm

from mable.cargo_bidding import TradingCompany, SimpleCompany
from mable.examples.companies import ScheduleProposal
from marshmallow import fields
import attrs
from ortools.sat.python import cp_model

from loguru import logger
from mable.cargo_bidding import Bid
from copy import deepcopy
from math import ceil
# import numpy as np
# from collections import defaultdict

class Solver:
    def __init__(self, headquarters):
        self.headquarters = headquarters

    def solve(self, trades, fleets):
        """
        Solve the problem of scheduling the trades. Input is a list of trades and output decision variables.
        time_step is the time step of current time
        """
        # process the trades and assign a unique id to each trade
        start_time = trades[0].time
        earliest_pickup = min(trade.time_window[0] for trade in trades)
        latest_dropoff = max(trade.time_window[3] for trade in trades)
        max_time = latest_dropoff - earliest_pickup
        trades_with_id = []
        for i, trade in enumerate(trades):
            trade_with_id = deepcopy(trade)
            setattr(trade_with_id, "id", i)
            # add travel time to the trade
            travel_distance = self.headquarters.get_network_distance(trade.origin_port, trade.destination_port)
            # travel_time = fleets[0].get_travel_time(travel_distance)
            setattr(trade_with_id, "travel_distance", travel_distance)
            trades_with_id.append(trade_with_id)

        model = cp_model.CpModel()
        # define decision variables
        assign = {}
        pickup_time = {}
        dropoff_time = {}
        for t, trade in enumerate(trades):
            for v, vessel in enumerate(fleets):
                assign[t, v] = model.NewBoolVar(f"assign_t{t}_v{v}")
            earliest_pickup = trade.time_window[0]
            latest_pickup = trade.time_window[1]
            earliest_dropoff = trade.time_window[2]
            latest_dropoff = trade.time_window[3]

            pickup_time[t] = model.NewIntVar(earliest_pickup, latest_pickup, f"pickup_time_t{t}")
            dropoff_time[t] = model.NewIntVar(earliest_dropoff, latest_dropoff, f"dropoff_time_t{t}")

        # define constraints
        # Constraint: each trade is either served by one vessel or unserved
        for t in range(len(trades)):
            model.Add(sum(assign[t, v] for v in range(len(fleets))) <= 1)

        # Constraint: Ensure first pickup time allows for travel from depot
        for v, vessel in enumerate(fleets):
            vessel_location = vessel.location  # Get the vessel's starting location

            # Store the is_first booleans for the AddAtMostOne constraint later
            is_first_trade_list_for_v = []

            for t in range(len(trades)):
                is_first_trade_for_v = model.NewBoolVar(f"is_first_{t}_for_{v}")
                is_first_trade_list_for_v.append(is_first_trade_for_v)

                # --- Calculate travel time (same as before) ---
                trade_origin = trades_with_id[t].origin_port
                travel_distance = self.headquarters.get_network_distance(vessel_location, trade_origin)
                travel_time = 0 # Default
                can_reach = True
                if travel_distance is None or travel_distance == float('inf'):
                    logger.warning(f"Vessel {v} cannot reach trade {t} origin {trade_origin} from {vessel_location}.")
                    can_reach = False
                    # If unreachable, this trade cannot be assigned to this vessel at all.
                    model.Add(assign[t, v] == 0)
                else:
                    travel_time = ceil(vessel.get_travel_time(travel_distance))

                    # --- Check for inherent infeasibility ---
                    # If the earliest possible arrival is already after the latest pickup window, it's impossible.
                    latest_pickup_time = trades_with_id[t].latest_pickup # Assuming this attribute exists
                    if travel_time > latest_pickup_time:
                         logger.warning(f"Vessel {v} cannot reach trade {t} ({trade_origin}) by latest pickup time {latest_pickup_time}. Travel time: {travel_time}.")
                         # Force assign[t, v] to be false if travel time exceeds latest pickup
                         model.Add(assign[t, v] == 0)
                         can_reach = False # Treat as unreachable for the first trade logic

                # --- Define is_first_trade_for_v ---

                # Condition 1: Must be assigned to this vessel.
                model.AddImplication(is_first_trade_for_v, assign[t, v])

                # Condition 2: No other assigned trade t_prime for vessel v has pickup_time[t_prime] < pickup_time[t]
                # Create a list of booleans, true if t_prime is earlier and assigned
                earlier_and_assigned_conditions = []
                for t_prime in range(len(trades)):
                    if t == t_prime: continue

                    # Aux Bool: is t_prime assigned to v?
                    t_prime_assigned = assign[t_prime, v] # Directly use the assign variable

                    # Aux Bool: is pickup_time[t_prime] < pickup_time[t]?
                    t_prime_earlier = model.NewBoolVar(f"aux_earlier_{t_prime}_{t}_{v}")
                    model.Add(pickup_time[t_prime] < pickup_time[t]).OnlyEnforceIf(t_prime_earlier)
                    model.Add(pickup_time[t_prime] >= pickup_time[t]).OnlyEnforceIf(t_prime_earlier.Not())

                    # Aux Bool: is t_prime assigned AND earlier?
                    t_prime_is_earlier_and_assigned = model.NewBoolVar(f"aux_earlier_assigned_{t_prime}_{t}_{v}")
                    model.AddBoolAnd([t_prime_assigned, t_prime_earlier]).OnlyEnforceIf(t_prime_is_earlier_and_assigned)
                    model.AddBoolOr([t_prime_assigned.Not(), t_prime_earlier.Not()]).OnlyEnforceIf(t_prime_is_earlier_and_assigned.Not())

                    earlier_and_assigned_conditions.append(t_prime_is_earlier_and_assigned)

                # is_first_trade_for_v is True IFF assign[t, v] is True AND ALL earlier_and_assigned_conditions are False.
                # Use AddBoolAnd to enforce the forward implication:
                model.AddBoolAnd([assign[t, v]] + [b.Not() for b in earlier_and_assigned_conditions]).OnlyEnforceIf(is_first_trade_for_v)

                # Explicitly enforce the backward implication (strengthen definition):
                # If assign[t,v] is false OR ANY earlier trade IS assigned, then is_first_trade_for_v MUST be false.
                model.AddBoolOr([assign[t, v].Not()] + earlier_and_assigned_conditions).OnlyEnforceIf(is_first_trade_for_v.Not())

                # --- Add the core constraint ---
                if can_reach: # Only add the constraint if reaching is possible
                    # pickup_time[t] >= travel_time IF is_first_trade_for_v
                    model.Add(pickup_time[t] >= start_time + travel_time).OnlyEnforceIf(is_first_trade_for_v)
                # else: model.Add(is_first_trade_for_v == 0) # This is implicitly handled by assign[t,v]==0


            # Constraint: At most one trade can be the first for vessel v
            model.AddAtMostOne(is_first_trade_list_for_v)


        # Constraint: the pickup time must be before the dropoff time, hard constraint
        for t, trade in enumerate(trades):
            for v, vessel in enumerate(fleets):
                loading_time = vessel.get_loading_time(trade.cargo_type, trade.amount)
                # unloading_time = loading_time
                # travel_distance = self.headquarters.get_network_distance(trade.origin_port, trade.destination_port)
                travel_distance = trades_with_id[t].travel_distance
                travel_time = vessel.get_travel_time(travel_distance)
                journey_duration = ceil(travel_time + loading_time)
                setattr(trades_with_id[t], "duration", journey_duration)
                # Create an interval for the journey
                # journey_interval = model.NewIntervalVar(
                #     pickup_time[t],               # start
                #     journey_duration,             # duration
                #     pickup_time[t] + journey_duration,  # end
                #     f'journey_{t}_{v}'            # name
                # )

                # Add constraint that dropoff time must be after journey end
                model.Add(pickup_time[t] + journey_duration <= dropoff_time[t]).OnlyEnforceIf(assign[t, v])
                model.Add(pickup_time[t] >= trade.time_window[0]).OnlyEnforceIf(assign[t, v]) # pickup time must be after the earliest pickup time
                model.Add(pickup_time[t] <= trade.time_window[1]).OnlyEnforceIf(assign[t, v]) # pickup time must be before the latest pickup time
                model.Add(dropoff_time[t] >= trade.time_window[2]).OnlyEnforceIf(assign[t, v]) # dropoff time must be after the earliest dropoff time
                model.Add(dropoff_time[t] <= trade.time_window[3]).OnlyEnforceIf(assign[t, v]) # dropoff time must be before the latest dropoff time

        
        # Constraint: Flexible pickup-dropoff sequencing with multiple cargoes
        for v, vessel in enumerate(fleets):
            for t1 in range(len(trades)):
                for t2 in range(t1 + 1, len(trades)): # Ensure t1 != t2 and avoid duplicates
                    # Bool: Are t1 and t2 both assigned to v?
                    both_assigned = model.NewBoolVar(f"both_assigned_{v}_{t1}_{t2}")
                    model.AddBoolAnd([assign[t1, v], assign[t2, v]]).OnlyEnforceIf(both_assigned)
                    model.AddBoolOr([assign[t1, v].Not(), assign[t2, v].Not()]).OnlyEnforceIf(both_assigned.Not())

                    # --- PICKUP ORDERING ---
                    # Bool: Is t1 picked up strictly before t2?
                    t1_pickup_before_t2 = model.NewBoolVar(f"t1_pickup_before_t2_{v}_{t1}_{t2}")
                    # Bool: Is t2 picked up strictly before t1?
                    t2_pickup_before_t1 = model.NewBoolVar(f"t2_pickup_before_t1_{v}_{t1}_{t2}")

                    # Link pickup order variables to pickup times
                    model.Add(pickup_time[t1] < pickup_time[t2]).OnlyEnforceIf(t1_pickup_before_t2)
                    model.Add(pickup_time[t2] < pickup_time[t1]).OnlyEnforceIf(t2_pickup_before_t1)

                    # If both assigned, exactly one must be picked up before the other
                    model.AddBoolOr([t1_pickup_before_t2, t2_pickup_before_t1]).OnlyEnforceIf(both_assigned)
                    # If not both assigned, set pickup order variables to false
                    model.Add(t1_pickup_before_t2 == 0).OnlyEnforceIf(both_assigned.Not())
                    model.Add(t2_pickup_before_t1 == 0).OnlyEnforceIf(both_assigned.Not())

                    # --- DROPOFF ORDERING (independent of pickup ordering) ---
                    # Bool: Is t1 dropped off strictly before t2?
                    t1_dropoff_before_t2 = model.NewBoolVar(f"t1_dropoff_before_t2_{v}_{t1}_{t2}")
                    # Bool: Is t2 dropped off strictly before t1?
                    t2_dropoff_before_t1 = model.NewBoolVar(f"t2_dropoff_before_t1_{v}_{t1}_{t2}")

                    # Link dropoff order variables to dropoff times
                    model.Add(dropoff_time[t1] < dropoff_time[t2]).OnlyEnforceIf(t1_dropoff_before_t2)
                    model.Add(dropoff_time[t2] < dropoff_time[t1]).OnlyEnforceIf(t2_dropoff_before_t1)

                    # If both assigned, exactly one must be dropped off before the other
                    model.AddBoolOr([t1_dropoff_before_t2, t2_dropoff_before_t1]).OnlyEnforceIf(both_assigned)
                    # If not both assigned, set dropoff order variables to false
                    model.Add(t1_dropoff_before_t2 == 0).OnlyEnforceIf(both_assigned.Not())
                    model.Add(t2_dropoff_before_t1 == 0).OnlyEnforceIf(both_assigned.Not())

                    # --- CAPACITY CHECK ---
                    # Ensure vessel has capacity for both trades if carried simultaneously
                    # For simplicity, assume a single cargo type check
                    cargo_type_t1 = trades[t1].cargo_type
                    cargo_type_t2 = trades[t2].cargo_type

                    # For trades with the same cargo type, check total capacity
                    if cargo_type_t1 == cargo_type_t2:
                        if vessel.capacity(cargo_type_t1) < trades[t1].amount + trades[t2].amount:
                            # Cannot carry both simultaneously - force sequential operation
                            # Either t1 must be dropped off before t2 is picked up,
                            # or t2 must be dropped off before t1 is picked up
                            sequential_operation = model.NewBoolVar(f"sequential_op_{v}_{t1}_{t2}")
                            model.AddBoolOr([
                                # t1 dropoff before t2 pickup
                                model.NewBoolVar(f"t1_dropoff_before_t2_pickup_{v}_{t1}_{t2}"),
                                # t2 dropoff before t1 pickup
                                model.NewBoolVar(f"t2_dropoff_before_t1_pickup_{v}_{t1}_{t2}")
                            ]).OnlyEnforceIf(sequential_operation)

                            # Add the actual time constraints for sequential operation
                            model.Add(pickup_time[t2] >= dropoff_time[t1]).OnlyEnforceIf(model.NewBoolVar(f"t1_dropoff_before_t2_pickup_{v}_{t1}_{t2}"))
                            model.Add(pickup_time[t1] >= dropoff_time[t2]).OnlyEnforceIf(model.NewBoolVar(f"t2_dropoff_before_t1_pickup_{v}_{t1}_{t2}"))

                            # Force sequential_operation if both assigned
                            model.AddImplication(both_assigned, sequential_operation)

                    # --- TIME CONSTRAINTS ---
                    # Case 1: t1 picked up first
                    load_time_t1 = ceil(vessel.get_loading_time(trades[t1].cargo_type, trades[t1].amount))
                    dist_t1_pickup_to_t2_pickup = self.headquarters.get_network_distance(trades[t1].origin_port, trades[t2].origin_port)
                    travel_t1_pickup_to_t2_pickup = ceil(vessel.get_travel_time(dist_t1_pickup_to_t2_pickup)) if dist_t1_pickup_to_t2_pickup is not None else float('inf')

                    # Minimum time between pickups if t1 is picked up first
                    min_pickup_separation_t1_first = load_time_t1 + travel_t1_pickup_to_t2_pickup

                    # Case 2: t2 picked up first
                    load_time_t2 = ceil(vessel.get_loading_time(trades[t2].cargo_type, trades[t2].amount))
                    dist_t2_pickup_to_t1_pickup = self.headquarters.get_network_distance(trades[t2].origin_port, trades[t1].origin_port)
                    travel_t2_pickup_to_t1_pickup = ceil(vessel.get_travel_time(dist_t2_pickup_to_t1_pickup)) if dist_t2_pickup_to_t1_pickup is not None else float('inf')

                    # Minimum time between pickups if t2 is picked up first
                    min_pickup_separation_t2_first = load_time_t2 + travel_t2_pickup_to_t1_pickup

                    # --- DROPOFF CONSTRAINTS ---
                    # Case 1: t1 dropped off first
                    unload_time_t1 = ceil(vessel.get_loading_time(trades[t1].cargo_type, trades[t1].amount)) # Assuming unloading time = loading time
                    dist_t1_dropoff_to_t2_dropoff = self.headquarters.get_network_distance(trades[t1].destination_port, trades[t2].destination_port)
                    travel_t1_dropoff_to_t2_dropoff = ceil(vessel.get_travel_time(dist_t1_dropoff_to_t2_dropoff)) if dist_t1_dropoff_to_t2_dropoff is not None else float('inf')

                    # Minimum time between dropoffs if t1 is dropped off first
                    min_dropoff_separation_t1_first = unload_time_t1 + travel_t1_dropoff_to_t2_dropoff

                    # Case 2: t2 dropped off first
                    unload_time_t2 = ceil(vessel.get_loading_time(trades[t2].cargo_type, trades[t2].amount)) # Assuming unloading time = loading time
                    dist_t2_dropoff_to_t1_dropoff = self.headquarters.get_network_distance(trades[t2].destination_port, trades[t1].destination_port)
                    travel_t2_dropoff_to_t1_dropoff = ceil(vessel.get_travel_time(dist_t2_dropoff_to_t1_dropoff)) if dist_t2_dropoff_to_t1_dropoff is not None else float('inf')

                    # Minimum time between dropoffs if t2 is dropped off first
                    min_dropoff_separation_t2_first = unload_time_t2 + travel_t2_dropoff_to_t1_dropoff

                    # --- ADD TIME CONSTRAINTS ---
                    # Pickup separation constraints
                    if min_pickup_separation_t1_first != float('inf'):
                        model.Add(pickup_time[t2] >= pickup_time[t1] + min_pickup_separation_t1_first).OnlyEnforceIf([both_assigned, t1_pickup_before_t2])
                    else:
                        model.AddImplication(both_assigned, t1_pickup_before_t2.Not())  # Prevent impossible sequencing

                    if min_pickup_separation_t2_first != float('inf'):
                        model.Add(pickup_time[t1] >= pickup_time[t2] + min_pickup_separation_t2_first).OnlyEnforceIf([both_assigned, t2_pickup_before_t1])
                    else:
                        model.AddImplication(both_assigned, t2_pickup_before_t1.Not())  # Prevent impossible sequencing

                    # Dropoff separation constraints
                    if min_dropoff_separation_t1_first != float('inf'):
                        model.Add(dropoff_time[t2] >= dropoff_time[t1] + min_dropoff_separation_t1_first).OnlyEnforceIf([both_assigned, t1_dropoff_before_t2])
                    else:
                        model.AddImplication(both_assigned, t1_dropoff_before_t2.Not())  # Prevent impossible sequencing

                    if min_dropoff_separation_t2_first != float('inf'):
                        model.Add(dropoff_time[t1] >= dropoff_time[t2] + min_dropoff_separation_t2_first).OnlyEnforceIf([both_assigned, t2_dropoff_before_t1])
                    else:
                        model.AddImplication(both_assigned, t2_dropoff_before_t1.Not())  # Prevent impossible sequencing

                    # --- Additional Constraint: Handling Pick-Pick-Drop Scenario ---
                    # This applies if t1 is picked up, then t2 is picked up, BEFORE t1 is dropped off.

                    # Bool: Is t2 picked up before t1 is dropped off?
                    # Create this variable ONLY if both trades could potentially be assigned (avoids unnecessary vars)
                    t2_pickup_before_t1_dropoff = model.NewBoolVar(f"aux_t2_pickup_before_t1_dropoff_{v}_{t1}_{t2}")
                    model.Add(pickup_time[t2] < dropoff_time[t1]).OnlyEnforceIf(t2_pickup_before_t1_dropoff)
                    model.Add(pickup_time[t2] >= dropoff_time[t1]).OnlyEnforceIf(t2_pickup_before_t1_dropoff.Not())


                    # Combined condition: both assigned AND t1 picked up before t2 AND t2 picked up before t1 dropped off
                    pick_pick_drop_condition_t1 = model.NewBoolVar(f"pick_pick_drop_cond_t1_{v}_{t1}_{t2}")
                    model.AddBoolAnd([
                        both_assigned,
                        t1_pickup_before_t2,           # t1 picked up first (variable already defined above)
                        t2_pickup_before_t1_dropoff    # t2 picked up before t1 dropoff
                    ]).OnlyEnforceIf(pick_pick_drop_condition_t1)
                    # Define negation for completeness
                    model.AddBoolOr([
                        both_assigned.Not(),
                        t1_pickup_before_t2.Not(),
                        t2_pickup_before_t1_dropoff.Not()
                    ]).OnlyEnforceIf(pick_pick_drop_condition_t1.Not())


                    # Calculate the minimum time required for t1 dropoff AFTER t2 pickup starts
                    # (loading t2 + travel from t2 origin to t1 destination)
                    load_time_t2_for_cond = ceil(vessel.get_loading_time(trades[t2].cargo_type, trades[t2].amount))
                    dist_t2_origin_to_t1_dest = self.headquarters.get_network_distance(trades[t2].origin_port, trades[t1].destination_port)
                    travel_t2o_t1d = 0 # Default
                    can_travel_t2o_t1d = True
                    if dist_t2_origin_to_t1_dest is None or dist_t2_origin_to_t1_dest == float('inf'):
                        can_travel_t2o_t1d = False
                        # If this travel is impossible, the pick-pick-drop sequence (t1 first) cannot happen
                        model.Add(pick_pick_drop_condition_t1 == 0) # Prevent this specific sequence
                    else:
                        travel_t2o_t1d = ceil(vessel.get_travel_time(dist_t2_origin_to_t1_dest))

                    # Minimum time from t2 pickup start until t1 dropoff can FINISH
                    # dropoff_time[t1] includes unload_time_t1.
                    # So, pickup_time[t2] + load_time_t2 + travel_t2o_t1d gives the earliest ARRIVAL at t1_dest after loading t2.
                    # The dropoff_time[t1] must be at least this arrival time + unload_time_t1
                    # However, since dropoff_time[t1] is defined as the completion time, we can simplify:
                    # dropoff_time[t1] >= pickup_time[t2] + load_time_t2 + travel_t2o_t1d
                    min_time_from_t2pickup_to_t1dropoff_start = load_time_t2_for_cond + travel_t2o_t1d


                    # Add the constraint: If the pick-pick-drop sequence occurs, enforce the time
                    if can_travel_t2o_t1d:
                        # dropoff_time[t1] must be >= pickup_time[t2] + load_time_t2 + travel(t2_origin -> t1_dest)
                        model.Add(dropoff_time[t1] >= pickup_time[t2] + ceil(min_time_from_t2pickup_to_t1dropoff_start)).OnlyEnforceIf(pick_pick_drop_condition_t1)


                    # --- Symmetrical Constraint: If t2 is picked up first, then t1, before t2 is dropped ---
                    # Bool: Is t1 picked up before t2 is dropped off?
                    t1_pickup_before_t2_dropoff = model.NewBoolVar(f"aux_t1_pickup_before_t2_dropoff_{v}_{t1}_{t2}")
                    model.Add(pickup_time[t1] < dropoff_time[t2]).OnlyEnforceIf(t1_pickup_before_t2_dropoff)
                    model.Add(pickup_time[t1] >= dropoff_time[t2]).OnlyEnforceIf(t1_pickup_before_t2_dropoff.Not())


                    # Combined condition: both assigned AND t2 picked up before t1 AND t1 picked up before t2 dropped off
                    pick_pick_drop_condition_t2 = model.NewBoolVar(f"pick_pick_drop_cond_t2_{v}_{t1}_{t2}")
                    model.AddBoolAnd([
                        both_assigned,
                        t2_pickup_before_t1,           # t2 picked up first (variable already defined above)
                        t1_pickup_before_t2_dropoff    # t1 picked up before t2 dropoff
                    ]).OnlyEnforceIf(pick_pick_drop_condition_t2)
                    model.AddBoolOr([
                        both_assigned.Not(),
                        t2_pickup_before_t1.Not(),
                        t1_pickup_before_t2_dropoff.Not()
                    ]).OnlyEnforceIf(pick_pick_drop_condition_t2.Not())

                    # Calculate the minimum time required for t2 dropoff AFTER t1 pickup starts
                    load_time_t1_for_cond = ceil(vessel.get_loading_time(trades[t1].cargo_type, trades[t1].amount))
                    dist_t1_origin_to_t2_dest = self.headquarters.get_network_distance(trades[t1].origin_port, trades[t2].destination_port)
                    travel_t1o_t2d = 0
                    can_travel_t1o_t2d = True
                    if dist_t1_origin_to_t2_dest is None or dist_t1_origin_to_t2_dest == float('inf'):
                         can_travel_t1o_t2d = False
                         model.Add(pick_pick_drop_condition_t2 == 0) # Prevent impossible sequence
                    else:
                         travel_t1o_t2d = ceil(vessel.get_travel_time(dist_t1_origin_to_t2_dest))

                    min_time_from_t1pickup_to_t2dropoff_start = load_time_t1_for_cond + travel_t1o_t2d

                    # Add the symmetrical constraint
                    if can_travel_t1o_t2d:
                         # dropoff_time[t2] must be >= pickup_time[t1] + load_time_t1 + travel(t1_origin -> t2_dest)
                         model.Add(dropoff_time[t2] >= pickup_time[t1] + int(min_time_from_t1pickup_to_t2dropoff_start)).OnlyEnforceIf(pick_pick_drop_condition_t2)

        # --- New Constraint: for two consecutive trades, the dropoff time of the first trade must be before the pickup time of the second trade ---
        idle_consumption_expr = []
        SCALE_FACTOR = 100.0 # Adjust this factor as needed
        for v, vessel in enumerate(fleets):
            # Pre-calculate vessel-specific values outside the inner loops
            idle_consumption_rate = ceil(vessel._propelling_engine._idle_consumption)
            scaled_idle_rate = ceil(idle_consumption_rate * SCALE_FACTOR)
            max_possible_inter_idle_cost = ceil(max_time * scaled_idle_rate) # Upper bound for inter-trade idle cost

            for t1 in range(len(trades)):
                for t2 in range(len(trades)):
                    if t1 == t2:
                        continue  # Skip same trade
                        
                    # Boolean: Are both trades assigned to this vessel?
                    both_assigned_seq = model.NewBoolVar(f"both_assigned_seq_{v}_{t1}_{t2}")
                    model.AddBoolAnd([assign[t1, v], assign[t2, v]]).OnlyEnforceIf(both_assigned_seq)
                    model.AddBoolOr([assign[t1, v].Not(), assign[t2, v].Not()]).OnlyEnforceIf(both_assigned_seq.Not())
                    
                    # Boolean: Is t1 dropoff (arrival) before t2 pickup?
                    t1_dropoff_before_t2_pickup_seq = model.NewBoolVar(f"t1_dropoff_before_t2_pickup_seq_{v}_{t1}_{t2}")
                    model.Add(dropoff_time[t1] < pickup_time[t2]).OnlyEnforceIf(t1_dropoff_before_t2_pickup_seq)
                    model.Add(dropoff_time[t1] >= pickup_time[t2]).OnlyEnforceIf(t1_dropoff_before_t2_pickup_seq.Not())
                    model.Add(t1_dropoff_before_t2_pickup_seq == 0).OnlyEnforceIf(both_assigned_seq.Not())
                    
                    # Calculate required times
                    unload_time_t1 = ceil(vessel.get_loading_time(trades[t1].cargo_type, trades[t1].amount))
                    dist_t1_dest_to_t2_origin = self.headquarters.get_network_distance(trades[t1].destination_port, trades[t2].origin_port)
                    
                    # Check if travel is possible
                    travel_time_t1d_t2o = 0
                    can_travel_t1d_t2o = True
                    if dist_t1_dest_to_t2_origin is None or dist_t1_dest_to_t2_origin == float('inf'):
                        can_travel_t1d_t2o = False
                        # If travel is impossible, this sequence cannot happen
                        model.AddImplication(both_assigned_seq, t1_dropoff_before_t2_pickup_seq.Not())
                    else:
                        travel_time_t1d_t2o = ceil(vessel.get_travel_time(dist_t1_dest_to_t2_origin))
                    
                    # Combined condition for sequential operation
                    sequential_flow = model.NewBoolVar(f"sequential_flow_{v}_{t1}_{t2}")
                    model.AddBoolAnd([both_assigned_seq, t1_dropoff_before_t2_pickup_seq]).OnlyEnforceIf(sequential_flow)
                    model.AddBoolOr([both_assigned_seq.Not(), t1_dropoff_before_t2_pickup_seq.Not()]).OnlyEnforceIf(sequential_flow.Not())
                    
                    # --- Inter-Trade Idle Time and Cost Calculation ---
                    inter_trade_idle_time = model.NewIntVar(0, max_time, f"inter_idle_{v}_{t1}_{t2}")
                    inter_trade_idle_cost_var = model.NewIntVar(0, max_possible_inter_idle_cost, f"inter_idle_cost_{v}_{t1}_{t2}")

                    if can_travel_t1d_t2o:
                        # Earliest time vessel can start pickup for t2 after finishing t1 dropoff, unloading, and traveling
                        min_pickup_time_t2 = dropoff_time[t1] + unload_time_t1 + travel_time_t1d_t2o

                        # Add the key constraint: pickup_time[t2] must be after the minimum required time
                        model.Add(pickup_time[t2] >= min_pickup_time_t2).OnlyEnforceIf(sequential_flow)
                        # This ensures that when t1 is processed before t2, t2's pickup respects the full sequence timing

                        # Calculate the idle time: actual pickup time - earliest possible pickup time
                        # Add(>=) handles the max(0, ...) implicitly
                        model.Add(inter_trade_idle_time >= pickup_time[t2] - min_pickup_time_t2).OnlyEnforceIf(sequential_flow)

                    # If the sequence doesn't happen (or is impossible), idle time is 0
                    model.Add(inter_trade_idle_time == 0).OnlyEnforceIf(sequential_flow.Not())

                    # Calculate the cost for this potential idle period
                    model.Add(inter_trade_idle_cost_var == inter_trade_idle_time * scaled_idle_rate)

                    # Add the inter-trade idle cost to the total idle cost expression
                    idle_consumption_expr.append(inter_trade_idle_cost_var)
                    # --- End Inter-Trade Idle Calculation ---


        # Constraint: capacity constraint
        # for v, vessel in enumerate(fleets):
        #     model.Add(sum(assign[t, v] * trade.amount for t, trade in enumerate(trades)) <= vessel.capacity)
        for v, vessel in enumerate(fleets):
            intervals = []
            demands = []
            for t, trade in enumerate(trades):
                interval = model.NewOptionalIntervalVar(
                    pickup_time[t],
                    trades_with_id[t].duration,  # duration: loading time + travel time
                    dropoff_time[t],
                    assign[t, v],
                    f'interval_{t}_{v}'
                )
                intervals.append(interval)
                demands.append(ceil(trade.amount))
            capacity_list = vessel.capacities_and_loading_rates
            model.AddCumulative(intervals, demands, ceil(capacity_list[0].capacity))

        # Modelling the idle time and ballast time
        # idle_consumption_expr = []
        ballast_consumption_expr = []

        # --- Initial Positioning and Idle/Ballast Consumption ---
        # current_time = 0 # Assuming planning starts at time 0. Adjust if needed.
        SCALE_FACTOR = 10 # Assuming you use 10 based on the previous code for ballast

        for v, vessel in enumerate(fleets):
            vessel_location = vessel.location  # Get the vessel's starting location
            is_first_trade_list_for_v = [] # For the AddAtMostOne constraint

            for t in range(len(trades)):
                is_first_trade_for_v = model.NewBoolVar(f"is_first_{t}_for_{v}")
                is_first_trade_list_for_v.append(is_first_trade_for_v)

                # --- Define is_first_trade_for_v (same logic as before) ---
                # 1. Must be assigned to v
                model.AddImplication(is_first_trade_for_v, assign[t, v])
                # 2. No other assigned trade t_prime has pickup_time[t_prime] < pickup_time[t]
                earlier_and_assigned_conditions = []
                for t_prime in range(len(trades)):
                    if t == t_prime: continue
                    t_prime_assigned = assign[t_prime, v]
                    t_prime_earlier = model.NewBoolVar(f"aux_earlier_{t_prime}_{t}_{v}")
                    model.Add(pickup_time[t_prime] < pickup_time[t]).OnlyEnforceIf(t_prime_earlier)
                    model.Add(pickup_time[t_prime] >= pickup_time[t]).OnlyEnforceIf(t_prime_earlier.Not())
                    t_prime_is_earlier_and_assigned = model.NewBoolVar(f"aux_earlier_assigned_{t_prime}_{t}_{v}")
                    model.AddBoolAnd([t_prime_assigned, t_prime_earlier]).OnlyEnforceIf(t_prime_is_earlier_and_assigned)
                    model.AddBoolOr([t_prime_assigned.Not(), t_prime_earlier.Not()]).OnlyEnforceIf(t_prime_is_earlier_and_assigned.Not())
                    earlier_and_assigned_conditions.append(t_prime_is_earlier_and_assigned)
                model.AddBoolAnd([assign[t, v]] + [b.Not() for b in earlier_and_assigned_conditions]).OnlyEnforceIf(is_first_trade_for_v)
                model.AddBoolOr([assign[t, v].Not()] + earlier_and_assigned_conditions).OnlyEnforceIf(is_first_trade_for_v.Not())

                # --- Calculate Initial Travel Time ---
                trade_origin = trades_with_id[t].origin_port
                travel_distance = self.headquarters.get_network_distance(vessel_location, trade_origin)
                initial_travel_time = 0 # Default
                can_reach = True
                if travel_distance is None or travel_distance == float('inf'):
                    can_reach = False
                    model.Add(assign[t, v] == 0) # Cannot assign if unreachable
                else:
                    initial_travel_time = ceil(vessel.get_travel_time(travel_distance))
                    latest_pickup_time = trades_with_id[t].latest_pickup
                    if initial_travel_time > latest_pickup_time:
                        can_reach = False
                        model.Add(assign[t, v] == 0) # Cannot assign if arrival is too late

                # --- Initial Ballast Cost ---
                initial_ballast_consumption = 0
                if can_reach:
                     initial_ballast_consumption = vessel.get_ballast_consumption(initial_travel_time, vessel.speed)

                scaled_initial_ballast_cons = ceil(initial_ballast_consumption * SCALE_FACTOR)
                temp_initial_ballast_var = model.NewIntVar(0, scaled_initial_ballast_cons, f"initial_ballast_{t}_{v}")
                # Set cost only if it's the first trade and reachable
                model.Add(temp_initial_ballast_var == scaled_initial_ballast_cons).OnlyEnforceIf(is_first_trade_for_v)
                model.Add(temp_initial_ballast_var == 0).OnlyEnforceIf(is_first_trade_for_v.Not())
                ballast_consumption_expr.append(temp_initial_ballast_var)


                # --- Initial Idle Time and Cost ---
                # Earliest arrival time at pickup location
                earliest_arrival_time = start_time + initial_travel_time

                # Create variable for initial idle time
                initial_idle_time = model.NewIntVar(0, max_time, f"initial_idle_{t}_{v}")

                # Calculate idle time: pickup_time[t] - earliest_arrival_time
                # This must be non-negative. The Add(>=) constraint handles the max(0, ...) implicitly.
                model.Add(initial_idle_time >= pickup_time[t] - earliest_arrival_time).OnlyEnforceIf(is_first_trade_for_v)

                # If not the first trade, initial idle time is 0
                model.Add(initial_idle_time == 0).OnlyEnforceIf(is_first_trade_for_v.Not())

                # Calculate initial idle cost
                idle_consumption_rate = ceil(vessel._propelling_engine._idle_consumption) # Per time unit
                scaled_idle_rate = ceil(idle_consumption_rate * SCALE_FACTOR)
                # Estimate max possible initial idle cost
                max_possible_idle_cost = ceil(max_time * scaled_idle_rate)

                temp_initial_idle_cost_var = model.NewIntVar(0, max_possible_idle_cost, f"initial_idle_cost_{t}_{v}")
                # Cost = rate * time
                model.Add(temp_initial_idle_cost_var == initial_idle_time * scaled_idle_rate) # Rate already scaled

                # Add this cost component to the total idle cost expression list
                idle_consumption_expr.append(temp_initial_idle_cost_var)

                # --- Core First Trade Pickup Time Constraint ---
                if can_reach:
                    # pickup_time[t] >= earliest_arrival_time is needed
                    # This is implicitly handled by initial_idle_time >= pickup_time[t] - earliest_arrival_time AND initial_idle_time >= 0
                    # But we can add it explicitly for clarity if desired:
                    # model.Add(pickup_time[t] >= earliest_arrival_time).OnlyEnforceIf(is_first_trade_for_v)
                    pass # Covered by idle time calculation


            # Constraint: At most one trade can be the first for vessel v
            model.AddAtMostOne(is_first_trade_list_for_v)

        # Calculate the idle and ballast cost during the trips, eg, between two consecutive trades

        # Constraint: capacity constraint
        # for v, vessel in enumerate(fleets):
        #     model.Add(sum(assign[t, v] * trade.amount for t, trade in enumerate(trades)) <= vessel.capacity)
        for v, vessel in enumerate(fleets):
            intervals = []
            demands = []
            for t, trade in enumerate(trades):
                interval = model.NewOptionalIntervalVar(
                    pickup_time[t],
                    trades_with_id[t].duration,  # duration: loading time + travel time
                    dropoff_time[t],
                    assign[t, v],
                    f'interval_{t}_{v}'
                )
                intervals.append(interval)
                demands.append(ceil(trade.amount))
            capacity_list = vessel.capacities_and_loading_rates
            model.AddCumulative(intervals, demands, ceil(capacity_list[0].capacity))

        # Objective: minimize the total cost
        fuel_expr = []
        penalty_expr = []
        for t, trade in enumerate(trades):
            for v, vessel in enumerate(fleets):
                travel_distance = self.headquarters.get_network_distance(trade.origin_port, trade.destination_port)
                loading_time = vessel.get_loading_time(trade.cargo_type, trade.amount)
                loading_cost = vessel.get_loading_consumption(loading_time)
                unloading_costs = vessel.get_unloading_consumption(loading_time)
                travel_time = vessel.get_travel_time(travel_distance)
                travel_cost = vessel.get_laden_consumption(travel_time, vessel.speed)
                total_cost = loading_cost + unloading_costs + travel_cost
                fuel_expr.append(assign[t, v] * total_cost)
            penalty_expr.append((1 - sum(assign[t, v] for v in range(len(fleets)))) * trade.amount * 10) # trade.amount is the penalty for unserved trades

        # idle cost
        total_idle_cost = sum(idle_consumption_expr)
        total_ballast_cost = sum(ballast_consumption_expr)
        # model.Minimize(sum(fuel_expr) + total_idle_cost + total_ballast_cost + sum(penalty_expr))
        model.Minimize(sum(fuel_expr) + sum(penalty_expr) + total_idle_cost + total_ballast_cost)
        # solve the problem
        solver = cp_model.CpSolver()
        status = solver.Solve(model)
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            print(f"Solution at time {start_time}:")
            for t, trade in enumerate(trades):
                served = False
                for v, vessel in enumerate(fleets):
                    if solver.Value(assign[t, v]):
                        served = True
                        print(f"Trade {t} is served by vessel {v}")
                        print(f"  Pickup:  {solver.Value(pickup_time[t])} at port {trade.origin_port}, earliest: {trade.time_window[0]}, latest: {trade.time_window[1]}")
                        print(f"  Dropoff: {solver.Value(dropoff_time[t])} at port {trade.destination_port}, earliest: {trade.time_window[2]}, latest: {trade.time_window[3]}")
                if not served:
                    print(f"Trade {t} is unserved (penalty {trade.amount})")
            print("Total cost:", solver.ObjectiveValue())
            # print the depot to the origin port of each vessel
            for v, vessel in enumerate(fleets):
                for t, trade in enumerate(trades):
                    if solver.Value(assign[t, v]):
                        travel_distance = self.headquarters.get_network_distance(vessel.location, trade.origin_port)
                        travel_time = vessel.get_travel_time(travel_distance)
                        print(f"Vessel {v} starts at depot {vessel.location} and ends at {trade.origin_port}, travel time: {travel_time}, start time: {trade.time}, arrival time: {trade.time + travel_time}")
            # Create a dictionary to store the assignment values
            assignment_values = {}

            # Extract assignment decisions (which vessel is assigned to which trade)
            for t in range(len(trades_with_id)):
                for v in range(len(fleets)):
                    # Check if this trade is assigned to this vessel
                    if solver.Value(assign[t, v]) == 1:
                        # Store the assignment (trade t is assigned to vessel v)
                        assignment_values[t] = v

            # Create a complete solution structure
            solution = {
                'status': 'OPTIMAL' if status == cp_model.OPTIMAL else str(status),
                'assignments': assignment_values,
                'pickup_times': {t: solver.Value(pickup_time[t]) for t in range(len(trades_with_id))},
                'dropoff_times': {t: solver.Value(dropoff_time[t]) for t in range(len(trades_with_id))},
                'objective_value': solver.ObjectiveValue()
                # Add other values you want to return
            }

            # If you created vessel_used variables, include those too
            # if 'vessel_used' in locals():
            #     solution['vessels_used'] = {v: solver.Value(vessel_used[v]) == 1
            #                              for v in range(len(fleets))}
            #     solution['num_vessels_used'] = sum(solution['vessels_used'].values())

            # If you tracked costs separately, include those
            # if 'total_idle_cost' in locals():
            #     solution['total_idle_cost'] = solver.Value(total_idle_cost) / SCALE_FACTOR  # Adjust if you used scaling

            # if 'total_ballast_cost' in locals():
            #     solution['total_ballast_cost'] = solver.Value(total_ballast_cost) / SCALE_FACTOR

            # if 'total_fixed_cost' in locals():
            #     solution['total_fixed_cost'] = solver.Value(total_fixed_cost) / SCALE_FACTOR

            return solution
        else:
            print("No solution found.")
            return None