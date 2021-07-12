# SC scenario logic sync

## `vlt` definition

**Original design**: `vlt` is the velocity of the vehicle. In other words, it is the number of cells that the vehicle could travel in one tick. The number of steps of the trip is calculated by the distance of the trip and the velocity of the vehicle, e.g., `# step = distance / velocity`.

This design itself is straightforward and sense-making, but there are several logic errors in its implementation. Check details in the source code.

**Current design**: `vlt`'s definition is simply redirected to "the number of steps of the trip". Therefore, the vehicle will reach the destination in exactly `vlt` steps regardless of the distance of the trip. Some related variables and logic are therefore become invalid (will not affect anything anymore). 

This design is a little bit weird, although it is the most simple way to make the current code work (only one line of code is modified).

**TODO**: we need to talk about what is the expected logic here.

## Units' `step()` order

The execution of each unit's `step()` method of a facility is actually executed serially, not in parallel. This will cause several logic issues, for example, if we send a `ConsumerAction` to a facility, the order will be placed at the **next tick**. We need to identify which ones are features and which ones are bugs.

Current order: 

- `StorageUnit`
- `DistributionUnit`
- `ProductUnit`
  - `ConsumerUnit`
  - `SellerUnit`
  - `ManufactureUnit`

## "Blocks" are never used

We have a concept of "block" in the world grid's setting but blocks are totally ignored when calculating the distance.

## Production speed & restriction

What does `output_units_per_lot` really mean? What are the restrictions of production speed?

## MISCs

Clarify every detail in the world config: what do they mean and whether they are necessary.

