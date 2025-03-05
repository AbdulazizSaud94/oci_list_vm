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

def list_mysql_databases(config, region, compartment_id):
    data = []
    region_config = config.copy()
    region_config["region"] = region
    mysql_client = oci.mysql.DbSystemClient(region_config)

    # Get a list of MySQL DB Systems
    mysql_instances = oci.pagination.list_call_get_all_results(
        mysql_client.list_db_systems, compartment_id=compartment_id
    ).data

    for db in mysql_instances:
        
        # Exclude DELETED DB Systems
        if db.lifecycle_state == "DELETED":
            continue  # Skip this DB System

        # Fetch full DB System details
        db_details = mysql_client.get_db_system(db.id).data


        date_created = db_details.time_created.replace(tzinfo=None) if db_details.time_created else None

        db_cost = get_mysql_cost(db.shape_name, db_details.data_storage_size_in_gbs)
        print(db_cost)
        record = {
            "region": region,
            "compartment_id": compartment_id,
            "db_system_name": db.display_name,
            "db_system_id": db.id,
            "shape": db.shape_name,  # Defines CPU & memory
            "data_storage_size_in_gbs": db_details.data_storage_size_in_gbs,  # Storage size (Now correctly retrieved)
            "availability_domain": db.availability_domain,  # Shows where it's hosted
            "is_highly_available": db.is_highly_available,  # HA setup flag
            "state": db.lifecycle_state,  # Current status
            "time_created": date_created,
            "db_cost": db_cost
        }
        data.append(record)

    return data


def get_mysql_cost(shape, storage_in_gbs):
    """
    Estimate the cost of an OCI MySQL instance based on its shape and storage.
    """
    # Pricing table (per hour costs per OCPU and memory for known shapes)
    pricing_table = {
        "MySQL.VM.Standard.E3": {"ocpus": 1, "memory": 8, "ocpu_price": 0.025, "memory_price": 0.0015},
        "MySQL.VM.Standard.E4": {"ocpus": 1, "memory": 16, "ocpu_price": 0.030, "memory_price": 0.002},
        "MySQL.HeatWave.VM.Standard.E3": {"ocpus": 16, "memory": 512, "ocpu_price": 0.04, "memory_price": 0.0025},
        "MySQL.HeatWave.VM.Standard.E4": {"ocpus": 16, "memory": 1024, "ocpu_price": 0.045, "memory_price": 0.003},
        "MySQL.HeatWave.BM.Standard.E3": {"ocpus": 32, "memory": 2048, "ocpu_price": 0.06, "memory_price": 0.004},
        "MySQL.VM.Standard2.8.120GB": {"ocpus": 8, "memory": 120, "ocpu_price": 0.05, "memory_price": 0.003},
        "MySQL.VM.Standard.E3.4.64GB": {"ocpus": 4, "memory": 64, "ocpu_price": 0.035, "memory_price": 0.002},
        "MySQL.HeatWave.VM.Standard": {"ocpus": 32, "memory": 2048, "ocpu_price": 0.06, "memory_price": 0.004},
        "MySQL.VM.Standard1.1": {"ocpus": 1, "memory": 7, "ocpu_price": 0.020, "memory_price": 0.0012},
        "MySQL.VM.Standard2.1": {"ocpus": 1, "memory": 15, "ocpu_price": 0.022, "memory_price": 0.0013},
        "MySQL.VM.Standard2.2": {"ocpus": 2, "memory": 30, "ocpu_price": 0.024, "memory_price": 0.0014},
        "MySQL.VM.Standard2.4": {"ocpus": 4, "memory": 60, "ocpu_price": 0.028, "memory_price": 0.0016},
        "MySQL.VM.Standard2.8": {"ocpus": 8, "memory": 120, "ocpu_price": 0.032, "memory_price": 0.0018},
        "MySQL.VM.Standard2.16": {"ocpus": 16, "memory": 240, "ocpu_price": 0.038, "memory_price": 0.002},
        "MySQL.VM.Standard2.24": {"ocpus": 24, "memory": 320, "ocpu_price": 0.042, "memory_price": 0.0022},
        "MySQL.2": {"ocpus": 2, "memory": 16, "ocpu_price": 0.03, "memory_price": 0.0015},
        "MySQL.4": {"ocpus": 4, "memory": 32, "ocpu_price": 0.035, "memory_price": 0.002},
        "MySQL.8": {"ocpus": 8, "memory": 64, "ocpu_price": 0.04, "memory_price": 0.0025},
        "MySQL.16": {"ocpus": 16, "memory": 128, "ocpu_price": 0.045, "memory_price": 0.003},
        "MySQL.32": {"ocpus": 32, "memory": 256, "ocpu_price": 0.05, "memory_price": 0.0035},
        "MySQL.48": {"ocpus": 48, "memory": 384, "ocpu_price": 0.055, "memory_price": 0.004},
        "MySQL.64": {"ocpus": 64, "memory": 512, "ocpu_price": 0.06, "memory_price": 0.0045},
        "MySQL.256": {"ocpus": 256, "memory": 2048, "ocpu_price": 0.07, "memory_price": 0.005}
    }
    
    
    storage_cost_per_gb_month = 0.0255  # USD per GB per month
    
    # Get shape details
    shape_details = pricing_table.get(shape, None)
    if not shape_details:
        return {"error": "Unknown shape"}
    
    ocpus = shape_details["ocpus"]
    memory_in_gbs = shape_details["memory"]
    ocpu_cost = ocpus * shape_details["ocpu_price"]
    memory_cost = memory_in_gbs * shape_details["memory_price"]
    
    # Storage cost (per month, converted to hourly)
    storage_cost_per_hour = (storage_in_gbs * storage_cost_per_gb_month) / 730
    
    # Total estimated cost per hour
    total_cost_per_hour = ocpu_cost + memory_cost + storage_cost_per_hour
    
    # Total estimated cost per month (730 hours)
    total_cost_per_month = total_cost_per_hour * 730
    
    return  round(total_cost_per_month, 2)
    


def main():
    # Load default config from ~/.oci/config or specify config_file and profile_name
    config = oci.config.from_file()  # e.g., oci.config.from_file("~/.oci/config","DEFAULT")
    identity_client = oci.identity.IdentityClient(config)
    tenancy_id = config["tenancy"]

    # Find all subscribed regions
    regions = get_all_subscribed_regions(config)

    # Get all compartments
    compartments = get_all_compartments(identity_client, tenancy_id)


    all_compute_data = []
    all_mysql_data = []

    # # Iterate over each region
    for region in regions:
        if region == "me-jeddah-1":
            print(f"Processing region: {region}")
            for compartment in compartments:

                print(compartment.name)
                compartment_name = compartment.name
                compartment_id = compartment.id

                # Compute Instances
                compute_data = list_instances_and_volumes(config, region, compartment_id)
                for rec in compute_data:
                    rec["compartment_name"] = compartment_name
                    all_compute_data.append(rec)

                # MySQL Databases
                mysql_data = list_mysql_databases(config, region, compartment_id)
                for rec in mysql_data:
                    rec["compartment_name"] = compartment_name
                    all_mysql_data.append(rec)

    df_compute = pd.DataFrame(all_compute_data)
    df_mysql = pd.DataFrame(all_mysql_data)

    # Reorder columns for neatness (adjust as desired)
    compute_columns_order = [
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
    df_compute = df_compute[compute_columns_order]





    output_file = "oci_inventory.xlsx"
    with pd.ExcelWriter(output_file) as writer:
        df_compute.to_excel(writer, sheet_name="Compute Instances", index=False)
        df_mysql.to_excel(writer, sheet_name="MySQL Databases", index=False)

    print(f"\nExported results to {output_file}")

if __name__ == "__main__":
    main()

