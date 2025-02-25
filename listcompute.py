#!/usr/bin/env python3

import oci
import pandas as pd




def get_all_subscribed_regions(config):
    """
    Return a list of region names the tenancy is subscribed to.
    """
    identity_client = oci.identity.IdentityClient(config)
    tenancy_id = config["tenancy"]
    subscribed_regions = identity_client.list_region_subscriptions(tenancy_id).data
    return [r.region_name for r in subscribed_regions]


def get_os_cost(os_name, ocpus):
    """
    Calculate additional OS licensing cost per month.
    """
    # Example Windows pricing per OCPU per hour (Adjust as needed)
    os_pricing_table = {
        "Windows": 0.092,  # Cost per OCPU per hour for Windows OS
        # Add other OS costs here if needed
    }

    # Default OS cost is 0
    os_cost_per_ocpu = os_pricing_table.get(os_name, 0)

    # Calculate monthly cost (730 hours per month)
    return ocpus * os_cost_per_ocpu * 730


def get_all_compartments(identity_client, tenancy_id):
    """
    Return a list of all active compartments in the tenancy (including root).
    Modify if you want to include deleted compartments or exclude root, etc.
    """
    compartments = []
    # We can use a paginator for large numbers of compartments:
    response = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        lifecycle_state="ACTIVE"
    )
    compartments_data = response.data

    # Add the root compartment as well (the tenancy itself)
    # Usually the root compartment has the same OCID as the tenancy
    root_compartment = identity_client.get_compartment(tenancy_id).data
    compartments.append(root_compartment)

    # Add the rest of compartments
    compartments.extend(compartments_data)

    return compartments

def list_instances_and_volumes(config, region, compartment_id):
    """
    List all instances in a given region and compartment,
    along with attached boot and block volumes.
    Returns a list of dictionary records.
    """
    data = []

    # Update config to use the target region
    region_config = config.copy()
    region_config["region"] = region

    compute_client = oci.core.ComputeClient(region_config)
    block_storage_client = oci.core.BlockstorageClient(region_config)

    # List all instances in the compartment
    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=compartment_id
    ).data

    for instance in instances:
        #Skip terminated instances if desired:
        if instance.lifecycle_state == "TERMINATED":
            continue
        
        date_created = instance.time_created.replace(tzinfo=None) if instance.time_created else None

        shape_config = instance.shape_config
        # shape_config can be None for older shapes, so handle that:
        ocpus = shape_config.ocpus if shape_config and shape_config.ocpus else "N/A"
        memory_in_gbs = shape_config.memory_in_gbs if shape_config and shape_config.memory_in_gbs else "N/A"


        # Collect attached volumes
        # We need the instance's availability domain for volume attachments
        ad = instance.availability_domain

        # List boot volume attachments for this instance
        boot_attachments = compute_client.list_boot_volume_attachments(
            compartment_id=compartment_id,
            availability_domain=ad,
            instance_id=instance.id
        ).data

        # # List block volume attachments for this instance
        block_attachments = compute_client.list_volume_attachments(
            compartment_id=compartment_id,
            availability_domain=ad,
            instance_id=instance.id
        ).data

        # Extract boot volume names/OCIDs
        boot_volumes = []
        boot_size = 0
        boot_cost = 0
        for bva in boot_attachments:
            boot_volumes.append(bva.boot_volume_id)
            bootinfo = block_storage_client.get_boot_volume(boot_volume_id=bva.boot_volume_id).data
            boot_size = boot_size + bootinfo.size_in_mbs
            boot_cost += get_storage_cost(bootinfo.size_in_mbs, region)


        # Extract block volume names/OCIDs
        block_volumes = []
        block_size = 0
        block_cost = 0
        for bva in block_attachments:
            block_volumes.append(bva.volume_id)
            blockinfo = block_storage_client.get_volume(volume_id=bva.volume_id).data
            block_size = block_size + blockinfo.size_in_mbs
            block_cost += get_storage_cost(blockinfo.size_in_mbs, region)

        

        image = compute_client.get_image(instance.image_id).data

        if instance.lifecycle_state == "STOPPED": estimated_compute_cost_per_month = 0
        else: estimated_compute_cost_per_month = get_instance_cost(instance.shape, ocpus, memory_in_gbs, region) * 730
        total_os_cost_per_month = get_os_cost(image.operating_system, ocpus)
        total_storage_cost_per_month = boot_cost + block_cost

        record = {
            "region": region,
            "compartment_id": compartment_id,
            "instance_name": instance.display_name,
            "instance_state" : instance.lifecycle_state,
            "instance_ocid": instance.id,
            "OS": image.operating_system,
            "shape": instance.shape,
            "ocpus": ocpus,
            "memory_in_gbs": memory_in_gbs,
            "boot_size_in_MBs" : boot_size,
            "block_size_in_MBs" : block_size,
            "boot_volumes": boot_volumes,
            "block_volumes": block_volumes,
            "date_created": date_created,
            "estimated_compute_cost_per_month": estimated_compute_cost_per_month,
            "estimated_os_cost_per_month": total_os_cost_per_month,
            "estimated_storage_cost_per_month": total_storage_cost_per_month,  # Add storage cost
            "total_cost_per_month": estimated_compute_cost_per_month + total_storage_cost_per_month + total_os_cost_per_month  # Compute total cost

        }
        data.append(record)

    return data

def get_storage_cost(size_in_mbs, region):
    """
    Calculate estimated cost of a storage volume in OCI.
    Prices are per GB per month, converted to per MB per hour.
    """
    # Example OCI storage pricing in USD per GB per month (Adjust as needed)
    pricing_table = {
        "standard_block": 0.0255,  # Standard Block Storage (per GB per month)
        "boot_volume": 0.0255,  # Boot Volumes (same rate as standard block storage)
    }

    # Convert size from MB to GB
    size_in_gbs = size_in_mbs / 1024 if size_in_mbs else 0

    # Convert monthly price to per-hour rate
    cost = size_in_gbs * pricing_table["standard_block"] 

    return cost

def get_instance_cost(shape, ocpus, memory_in_gbs, region):
    """
    Estimate the cost of an OCI compute instance based on its shape, OCPUs, and memory.
    Prices are region-dependent and may need to be updated.
    """

    # Example pricing in USD per hour (Adjust based on OCI's latest pricing)
    pricing_table = {
      "VM.Standard3.Flex": {"ocpu": 0.04, "memory": 0.0015},  # Per OCPU and GB per hour
        "VM.Standard.E3.Flex": {"ocpu": 0.025, "memory": 0.0015},
        "VM.Standard.E4.Flex": {"ocpu": 0.025, "memory": 0.0015},
        "VM.Standard.E5.Flex": {"ocpu": 0.03, "memory": 0.002},
        "BM.Standard.E3.128": {"fixed": 2.88},  # Fixed cost per hour
        "BM.Standard.E4.128": {"fixed": 3.2},
    }

    # Convert ocpus/memory to float (in case they are strings)
    ocpus = float(ocpus) if ocpus != "N/A" else 0
    memory_in_gbs = float(memory_in_gbs) if memory_in_gbs != "N/A" else 0

    # Check if the shape exists in our pricing table
    if shape in pricing_table:
        if "fixed" in pricing_table[shape]:  # For fixed cost shapes
            return pricing_table[shape]["fixed"]
        else:  # For flexible shapes
            return (ocpus * pricing_table[shape]["ocpu"]) + (memory_in_gbs * pricing_table[shape]["memory"])

    return 0  # Default if shape is unknown

def main():
    # Load default config from ~/.oci/config or specify config_file and profile_name
    config = oci.config.from_file()  # e.g., oci.config.from_file("~/.oci/config","DEFAULT")
    identity_client = oci.identity.IdentityClient(config)
    tenancy_id = config["tenancy"]

    # Find all subscribed regions
    regions = get_all_subscribed_regions(config)

    # Get all compartments
    compartments = get_all_compartments(identity_client, tenancy_id)


    all_data = []

    # # Iterate over each region
    for region in regions:
        if region == "me-jeddah-1":
            print(f"Processing region: {region}")
            for compartment in compartments:

                print(compartment.name)
                compartment_name = compartment.name
                compartment_id = compartment.id
                instance_data = list_instances_and_volumes(config, region, compartment_id)

                # Convert each record to a final dictionary that includes the compartment_name
                for rec in instance_data:
                    rec["compartment_name"] = compartment_name
                    all_data.append(rec)




    # Convert to Pandas DataFrame
    df = pd.DataFrame(all_data)

    # Reorder columns for neatness (adjust as desired)
    columns_order = [
        "region",
        "compartment_name",
        "compartment_id",
        "instance_state",
        "instance_name",
        "instance_ocid",
        "OS",
        "shape",
        "ocpus",
        "memory_in_gbs",
        "boot_size_in_MBs",
        "block_size_in_MBs",
        "boot_volumes",
        "block_volumes",
        "date_created",
        "estimated_compute_cost_per_month",
        "estimated_os_cost_per_month",
        "estimated_storage_cost_per_month",  # Add this column
        "total_cost_per_month"  # Add this column
    ]
    df = df[columns_order]

    # Export to Excel
    output_file = "oci_compute_inventory.xlsx"
    df.to_excel(output_file, index=False)
    print(f"\nExported results to {output_file}")

if __name__ == "__main__":
    main()

